from collections import namedtuple
from contextlib import ExitStack

import tensorflow as tf
from tensorflow.contrib.gan.python.train import _convert_tensor_or_l_or_d, _use_aux_loss, _validate_aux_loss_weight, \
  RunTrainOpsHook
from tensorflow.contrib.training.python.training import training

import data_provider
import networks
from namedtuples import StackGANModel, GeneratorLoss, DiscriminatorLoss, GeneratorTrainOp, DiscriminatorTrainOps
from util import _compute_mean_covariance, _tensor_pool_adjusted_model

# Aliases
tfgan = tf.contrib.gan

flags = tf.flags
flags.DEFINE_integer('stack_depth', 3,
                     'Defines the size of the GAN stack: ' +
                     'the number of (discriminator, generator) stages.')

flags.DEFINE_integer('batch_size', 8, 'The number of images in each batch.')  # 24

flags.DEFINE_integer('noise_dim', 64,  # 100
                     'Dimension of the input for the first generator.')

flags.DEFINE_float('color_loss', 50, 'Weight of color loss.')

flags.DEFINE_float('generator_lr', 0.0001, 'Generator learning rate.')  # 0.0002

flags.DEFINE_boolean('do_lr_decay', True, 'Whether or not to decay the generator\'s learning rate.')

flags.DEFINE_integer('decay_steps', 100000, 'After how many steps to decay the learning rate.')

flags.DEFINE_float('decay_rate', 0.9, 'How much of the learning rate to retain when decaying.')

flags.DEFINE_float('discriminator_lr', 0.0001, 'Discriminator learning rate.')  # 0.0002

flags.DEFINE_float('gradient_penalty', 1, 'Gradient penalty weight.')

flags.DEFINE_boolean('apply_batch_norm', False, 'Whether or not to apply batch normalization.')

flags.DEFINE_string('train_log_dir', '/tmp/cifar-stackgan-3stage',
                    'Directory to write event logs to.')

flags.DEFINE_string('dataset_dir', '/tmp/cifar-data', 'Location of data.')

flags.DEFINE_integer('max_number_of_steps', 1000000,  # num_samples / batch_size * 5 * 120 = 180000
                     'The maximum number of gradient steps.')
FLAGS = flags.FLAGS


def main(_):
  if not tf.gfile.Exists(FLAGS.train_log_dir):
    tf.gfile.MakeDirs(FLAGS.train_log_dir)

  # Get training data
  images = data_provider.get_training_data_iterator(FLAGS.batch_size, FLAGS.dataset_dir)

  def _get_or_create_gen_super_scope(super_scope):
    if not super_scope:
      with ExitStack() as stack:
        super_scope = stack.enter_context(tf.variable_scope('Generator', reuse=tf.AUTO_REUSE))
    return super_scope

  def gan_model(  # Lambdas defining models.
      generator_fn,
      discriminator_fn,
      # Real data and conditioning.
      real_data,
      generator_input_fn,
      # Stage (depth in stack).
      stage,
      generator_super_scope=None,
      # Options.
      check_shapes=True):
    current_stage_generator_scope = 'Generator_stage_' + str(stage)
    current_stage_discriminator_scope = 'Discriminator_stage_' + str(stage)

    # Wrap generator in super scope.
    generator_super_scope = _get_or_create_gen_super_scope(generator_super_scope)
    with tf.variable_scope(generator_super_scope):
      with tf.name_scope(generator_super_scope.original_name_scope):
        with tf.variable_scope(
            current_stage_generator_scope, reuse=tf.AUTO_REUSE) as current_gen_scope:
          print(tf.get_variable_scope().name)
          # Nested scope, specific to this generator stage.
          generator_inputs = generator_input_fn()
          generator_inputs = _convert_tensor_or_l_or_d(generator_inputs)
          kwargs = {'final_size': 2 ** (5 + stage), 'apply_batch_norm': FLAGS.apply_batch_norm}
          generated_data, generator_hidden_code = generator_fn(generator_inputs, **kwargs)

    # Discriminate generated and real data.
    with tf.variable_scope(
        current_stage_discriminator_scope, reuse=tf.AUTO_REUSE) as dis_scope:
      discriminator_gen_outputs = discriminator_fn(generated_data, None, apply_batch_norm=FLAGS.apply_batch_norm)
    with tf.variable_scope(dis_scope):
      with tf.name_scope(dis_scope.original_name_scope):
        real_data = tf.convert_to_tensor(real_data)
        discriminator_real_outputs = discriminator_fn(real_data, None, apply_batch_norm=FLAGS.apply_batch_norm)

    if check_shapes:
      if not generated_data.shape.is_compatible_with(real_data.shape):
        raise ValueError(
          'Generator output shape (%s) must be the same shape as real data '
          '(%s).' % (generated_data.shape, real_data.shape))

    # Get model-specific variables.
    generator_variables = tf.trainable_variables(generator_super_scope.name)
    discriminator_variables = tf.trainable_variables(dis_scope.name)

    return StackGANModel(
      generator_inputs,
      generated_data,
      generator_variables,
      generator_super_scope,
      generator_fn,
      real_data,
      discriminator_real_outputs,
      discriminator_gen_outputs,
      discriminator_variables,
      dis_scope,
      discriminator_fn,
      generator_hidden_code,
      stage)

  model_names = ['stage_' + str(i) for i in range(FLAGS.stack_depth)]
  GANModels = namedtuple('GANModels', model_names)

  """#### Instantiate GANModel tuples"""

  def _get_generator_input_for_stage(i, noise):
    assert isinstance(i, int)

    def get_input():
      # Input into first stage is z ~ p_noise.
      # Input for stage i generator is the hidden code outputted by stage (i-1) +
      # conditioning (just noise for image generation task).
      return [gan_models[i - 1].generator_hidden_code, noise] if i else noise

    return get_input

  def _get_real_data_for_stage(i):
    resolution = 2 ** (5 + i)
    current_res_images = tf.image.resize_images(images, size=[resolution, resolution])
    current_res_images.set_shape([FLAGS.batch_size, resolution, resolution, 3])
    return current_res_images

  noise = tf.random_normal([FLAGS.batch_size, FLAGS.noise_dim])
  gan_models = []
  for i in range(FLAGS.stack_depth):
    kwargs = {'generator_input_fn': _get_generator_input_for_stage(i, noise),
              'real_data': _get_real_data_for_stage(i),
              'generator_super_scope': gan_models[-1].generator_scope if i > 0 else None,
              'stage': i}
    current_model = gan_model(
      networks.generator,
      networks.discriminator,
      **kwargs)
    gan_models.append(current_model)
    tfgan.eval.add_gan_model_image_summaries(current_model, grid_size=2)

  gan_models = GANModels(*gan_models)

  """Losses"""

  # Color loss (requires gan_models to be in scope)
  def color_loss(models):
    means = []
    covariances = []
    total_color_loss = 0

    for gan_model in models:
      mu, cov = _compute_mean_covariance(gan_model.generated_data)
      means.append(mu)
      covariances.append(cov)
    stack_depth = FLAGS.stack_depth
    assert len(means) == len(covariances) == stack_depth

    for i in range(stack_depth - 1):
      # Elements at position i and (i + 1) aren't
      # the last two elements in the list.
      like_mu = FLAGS.color_loss * tf.losses.mean_squared_error(
        means[i], means[i + 1])
      like_cov = FLAGS.color_loss * 5 * tf.losses.mean_squared_error(
        covariances[i], covariances[i + 1])
      total_color_loss += like_mu + like_cov

    return FLAGS.color_loss * total_color_loss

    #  sum_mu = tf.summary.scalar('G_like_mu2', like_mu2.data[0])
    #  self.summary_writer.add_summary(sum_mu, count)
    #  sum_cov = summary.scalar('G_like_cov2', like_cov2.data[0])
    #  self.summary_writer.add_summary(sum_cov, count)
    #  if self.num_Ds > 2:
    #      sum_mu = summary.scalar('G_like_mu1', like_mu1.data[0])
    #      self.summary_writer.add_summary(sum_mu, count)
    #      sum_cov = summary.scalar('G_like_cov1', like_cov1.data[0])
    #      self.summary_writer.add_summary(sum_cov, count)

  """#### Dis and gen losses"""

  tfgan_losses = tfgan.losses

  def dis_loss(
      model,
      discriminator_loss_fn=tfgan_losses.wasserstein_discriminator_loss,
      # Auxiliary losses.
      gradient_penalty_weight=None,
      gradient_penalty_epsilon=1e-10,
      mutual_information_penalty_weight=None,
      aux_cond_generator_weight=None,
      aux_cond_discriminator_weight=None,
      tensor_pool_fn=None,
      # Options.
      add_summaries=True):
    """Returns losses necessary to train discriminator.
    Args:
      model: A GANModels tuple containing models for each stage.
      discriminator_loss_fn: The loss function on the discriminator. Takes a
        GANModel tuple.
      gradient_penalty_weight: If not `None`, must be a non-negative Python number
        or Tensor indicating how much to weight the gradient penalty. See
        https://arxiv.org/pdf/1704.00028.pdf for more details.
      gradient_penalty_epsilon: If `gradient_penalty_weight` is not None, the
        small positive value used by the gradient penalty function for numerical
        stability. Note some applications will need to increase this value to
        avoid NaNs.
      mutual_information_penalty_weight: If not `None`, must be a non-negative
        Python number or Tensor indicating how much to weight the mutual
        information penalty. See https://arxiv.org/abs/1606.03657 for more
        details.
      aux_cond_generator_weight: If not None: add a classification loss as in
        https://arxiv.org/abs/1610.09585
      aux_cond_discriminator_weight: If not None: add a classification loss as in
        https://arxiv.org/abs/1610.09585
      tensor_pool_fn: A function that takes (generated_data, generator_inputs),
        stores them in an internal pool and returns previous stored
        (generated_data, generator_inputs). For example
        `tf.gan.features.tensor_pool`. Defaults to None (not using tensor pool).
      add_summaries: Whether or not to add summaries for the losses.
    Returns:
      A DiscriminatorLoss 1-tuple of (discriminator_loss). Includes
      regularization losses.
    Raises:
      ValueError: If any of the auxiliary loss weights is provided and negative.
      ValueError: If `mutual_information_penalty_weight` is provided, but the
        `model` isn't an `InfoGANModel`.
    """
    # Validate arguments.
    gradient_penalty_weight = _validate_aux_loss_weight(gradient_penalty_weight,
                                                        'gradient_penalty_weight')
    mutual_information_penalty_weight = _validate_aux_loss_weight(
      mutual_information_penalty_weight, 'infogan_weight')
    aux_cond_generator_weight = _validate_aux_loss_weight(
      aux_cond_generator_weight, 'aux_cond_generator_weight')
    aux_cond_discriminator_weight = _validate_aux_loss_weight(
      aux_cond_discriminator_weight, 'aux_cond_discriminator_weight')

    # Verify configuration for mutual information penalty
    if (_use_aux_loss(mutual_information_penalty_weight) and
          not isinstance(model, tfgan.InfoGANModel)):
      raise ValueError(
        'When `mutual_information_penalty_weight` is provided, `model` must be '
        'an `InfoGANModel`. Instead, was %s.' % type(model))

    # Verify configuration for mutual auxiliary condition loss (ACGAN).
    if ((_use_aux_loss(aux_cond_generator_weight) or
           _use_aux_loss(aux_cond_discriminator_weight)) and
          not isinstance(model, tfgan.ACGANModel)):
      raise ValueError(
        'When `aux_cond_generator_weight` or `aux_cond_discriminator_weight` '
        'is provided, `model` must be an `ACGANModel`. Instead, was %s.' %
        type(model))

    # Create standard losses.
    dis_loss = discriminator_loss_fn(
      _tensor_pool_adjusted_model(model, tensor_pool_fn),
      add_summaries=add_summaries)

    # Add optional extra losses.
    if _use_aux_loss(gradient_penalty_weight):
      gp_loss = tfgan_losses.wasserstein_gradient_penalty(
        model, epsilon=gradient_penalty_epsilon, add_summaries=add_summaries)
      dis_loss += gradient_penalty_weight * gp_loss
    if _use_aux_loss(mutual_information_penalty_weight):
      info_loss = tfgan_losses.mutual_information_penalty(
        model, add_summaries=add_summaries)
      dis_loss += mutual_information_penalty_weight * info_loss
    if _use_aux_loss(aux_cond_generator_weight):
      ac_gen_loss = tfgan_losses.acgan_generator_loss(
        model, add_summaries=add_summaries)
    if _use_aux_loss(aux_cond_discriminator_weight):
      ac_disc_loss = tfgan_losses.acgan_discriminator_loss(
        model, add_summaries=add_summaries)
      dis_loss += aux_cond_discriminator_weight * ac_disc_loss
    # Gathers auxiliary losses.
    if model.discriminator_scope:
      dis_reg_loss = tf.losses.get_regularization_loss(
        model.discriminator_scope.name)
    else:
      dis_reg_loss = 0

    return DiscriminatorLoss(dis_loss + dis_reg_loss)

  def gen_loss(
      models,
      generator_loss_fn=tfgan_losses.wasserstein_generator_loss,
      # Auxiliary losses.
      mutual_information_penalty_weight=None,
      aux_cond_generator_weight=None,
      # Options.
      add_summaries=True):
    """Returns losses necessary to train generator.
    Args:
      model: A GANModel tuple.
      generator_loss_fn: The loss function on the generator. Takes a
        GANModel tuple.
      mutual_information_penalty_weight: If not `None`, must be a non-negative
        Python number or Tensor indicating how much to weight the mutual
        information penalty. See https://arxiv.org/abs/1606.03657 for more
        details.
      aux_cond_generator_weight: If not None: add a classification loss as in
        https://arxiv.org/abs/1610.09585
      add_summaries: Whether or not to add summaries for the losses.
    Returns:
      A GeneratorLoss 1-tuple of (generator_loss). Includes
      regularization losses.
    Raises:
      ValueError: If any of the auxiliary loss weights is provided and negative.
      ValueError: If `mutual_information_penalty_weight` is provided, but the
        `model` isn't an `InfoGANModel`.
    """
    # Validate arguments.
    # gradient_penalty_weight = _validate_aux_loss_weight(gradient_penalty_weight,
    #                                                    'gradient_penalty_weight')
    mutual_information_penalty_weight = _validate_aux_loss_weight(
      mutual_information_penalty_weight, 'infogan_weight')
    aux_cond_generator_weight = _validate_aux_loss_weight(
      aux_cond_generator_weight, 'aux_cond_generator_weight')

    # Verify configuration for mutual information penalty
    if (_use_aux_loss(mutual_information_penalty_weight) and
          not isinstance(model, tfgan.InfoGANModel)):
      raise ValueError(
        'When `mutual_information_penalty_weight` is provided, `model` must be '
        'an `InfoGANModel`. Instead, was %s.' % type(model))

    # Verify configuration for mutual auxiliary condition loss (ACGAN).
    if _use_aux_loss(aux_cond_generator_weight and
                         not isinstance(model, tfgan.ACGANModel)):
      raise ValueError(
        'When `aux_cond_generator_weight` or `aux_cond_discriminator_weight` '
        'is provided, `model` must be an `ACGANModel`. Instead, was %s.' %
        type(model))

    ### TODO: Verify for StackGAN

    # Create standard losses.
    gen_loss = 0

    ### TODO: use _use_aux_loss helper
    if FLAGS.color_loss > 0:
      # Compute color preserve losses
      color_loss_value = color_loss(models)
    else:
      color_loss_value = 0
    gen_loss += color_loss_value

    for i in range(FLAGS.stack_depth):
      with tf.name_scope('loss_stage_' + str(i)):
        gen_loss += generator_loss_fn(models[i], add_summaries=add_summaries)

        # Add optional extra losses.
        if _use_aux_loss(mutual_information_penalty_weight):
          info_loss = tfgan_losses.mutual_information_penalty(
            models[i], add_summaries=add_summaries)
          gen_loss += mutual_information_penalty_weight * info_loss
        if _use_aux_loss(aux_cond_generator_weight):
          ac_gen_loss = tfgan_losses.acgan_generator_loss(
            models[i], add_summaries=add_summaries)
          gen_loss += aux_cond_generator_weight * ac_gen_loss

    # Gathers auxiliary losses.
    if models[-1].generator_scope:
      gen_reg_loss = tf.losses.get_regularization_loss(models[-1].generator_scope)
    else:
      gen_reg_loss = 0

    return GeneratorLoss(gen_loss + gen_reg_loss)

  """#### Instantiate losses"""

  # Need to optimize discriminator at each stage independently,
  # so we add a loss for each discriminator to this list.
  # Seperate optimizers need to optimize
  # each loss in this list (DiscriminatorTrainOps).
  dis_losses = []
  for i in range(FLAGS.stack_depth):
    with tf.variable_scope(gan_models[i].discriminator_scope):
      with tf.name_scope(gan_models[i].discriminator_scope.original_name_scope):
        print(tf.get_variable_scope().name)
        with tf.variable_scope('losses'):
          current_stage_dis_loss = dis_loss(
            gan_models[i],
            discriminator_loss_fn=tfgan.losses.wasserstein_discriminator_loss,
            gradient_penalty_weight=FLAGS.gradient_penalty)
          dis_losses.append(current_stage_dis_loss)

  # Only a need for one overall generator loss, as generator is optimized once
  # per training step in which all discriminator stages are optimized.
  with tf.variable_scope(gan_models[-1].generator_scope):
    with tf.name_scope(gan_models[-1].generator_scope.original_name_scope):
      with tf.variable_scope('loss'):
        gen_loss_tuple = gen_loss(
          gan_models,
          generator_loss_fn=tfgan.losses.wasserstein_generator_loss
        )

  """#### Dis and gen train ops"""

  def _get_dis_update_ops(kwargs, dis_scope, check_for_unused_ops=True):
    """Gets discriminator update ops.
    Args:
      kwargs: A dictionary of kwargs to be passed to `create_train_op`.
        `update_ops` is removed, if present.
      dis_scope: A scope for the discriminator.
      check_for_unused_ops: A Python bool. If `True`, throw Exception if there are
        unused update ops.
    Returns:
      discriminator update ops.
    Raises:
      ValueError: If there are update ops outside of the generator or
        discriminator scopes.
    """
    if 'update_ops' in kwargs:
      update_ops = set(kwargs['update_ops'])
      del kwargs['update_ops']
    else:
      update_ops = set(tf.get_collection(tf.GraphKeys.UPDATE_OPS))

    all_dis_ops = set(tf.get_collection(tf.GraphKeys.UPDATE_OPS, dis_scope))

    # if check_for_unused_ops:
    #  unused_ops = update_ops - all_gen_ops - all_dis_ops
    #  if unused_ops:
    #    raise ValueError('There are unused update ops: %s' % unused_ops)

    dis_update_ops = list(all_dis_ops & update_ops)

    return dis_update_ops

  def _get_gen_update_ops(kwargs, gen_scope, check_for_unused_ops=True):
    """Gets generator update ops.
    Args:
      kwargs: A dictionary of kwargs to be passed to `create_train_op`.
        `update_ops` is removed, if present.
      gen_scope: A scope for the generator.
      check_for_unused_ops: A Python bool. If `True`, throw Exception if there are
        unused update ops.
    Returns:
      generator update ops
    Raises:
      ValueError: If there are update ops outside of the generator or
        discriminator scopes.
    """
    if 'update_ops' in kwargs:
      update_ops = set(kwargs['update_ops'])
      del kwargs['update_ops']
    else:
      update_ops = set(tf.get_collection(tf.GraphKeys.UPDATE_OPS))

    all_gen_ops = set(tf.get_collection(tf.GraphKeys.UPDATE_OPS, gen_scope))

    # if check_for_unused_ops:
    #  unused_ops = update_ops - all_gen_ops - all_dis_ops
    #  if unused_ops:
    #    raise ValueError('There are unused update ops: %s' % unused_ops)

    gen_update_ops = list(all_gen_ops & update_ops)

    return gen_update_ops

  def generator_train_op(
      model,
      loss,
      optimizer,
      check_for_unused_update_ops=True,
      # Optional args to pass directly to the `create_train_op`.
      **kwargs):
    # return GeneratorTrainOps tuple with one gen train op in generator_train_op field
    gen_update_ops = _get_gen_update_ops(
      kwargs, model.generator_scope.name,
      check_for_unused_update_ops)

    generator_global_step = None
    # if isinstance(generator_optimizer,
    #              sync_replicas_optimizer.SyncReplicasOptimizer):
    # TODO(joelshor): Figure out a way to get this work without including the
    # dummy global step in the checkpoint.
    # WARNING: Making this variable a local variable causes sync replicas to
    # hang forever.
    #  generator_global_step = variable_scope.get_variable(
    #      'dummy_global_step_generator',
    #      shape=[],
    #      dtype=global_step.dtype.base_dtype,
    #      initializer=init_ops.zeros_initializer(),
    #      trainable=False,
    #      collections=[ops.GraphKeys.GLOBAL_VARIABLES])
    #  gen_update_ops += [generator_global_step.assign(global_step)]
    with tf.name_scope('generator_train'):
      gen_train_op = training.create_train_op(
        total_loss=loss.generator_loss,
        optimizer=optimizer,
        variables_to_train=model.generator_variables,
        global_step=generator_global_step,
        update_ops=gen_update_ops,
        **kwargs)

    return GeneratorTrainOp(gen_train_op)

  def discriminator_train_ops(
      models,
      losses,
      optimizer,
      check_for_unused_update_ops=True,
      # Optional args to pass directly to the `create_train_op`.
      **kwargs):
    # return DiscriminatorTrainOps tuple with one train op per discriminator in the discriminator_train_op field
    dis_update_ops = []
    for i in range(FLAGS.stack_depth):
      current_dis_update_ops = _get_dis_update_ops(
        kwargs, models[i].discriminator_scope.name,
        check_for_unused_update_ops)
      dis_update_ops.append(current_dis_update_ops)

    discriminator_global_step = None
    # if isinstance(discriminator_optimizer,
    #              sync_replicas_optimizer.SyncReplicasOptimizer):
    # See comment above `generator_global_step`.
    #  discriminator_global_step = variable_scope.get_variable(
    #      'dummy_global_step_discriminator',
    #      shape=[],
    #      dtype=global_step.dtype.base_dtype,
    #      initializer=init_ops.zeros_initializer(),
    #      trainable=False,
    #      collections=[ops.GraphKeys.GLOBAL_VARIABLES])
    #  dis_update_ops += [discriminator_global_step.assign(global_step)]
    disc_train_ops = []
    for i in range(FLAGS.stack_depth):
      with tf.name_scope('discriminator_train'):
        current_disc_train_op = training.create_train_op(
          total_loss=losses[i].discriminator_loss,
          optimizer=optimizer,
          variables_to_train=models[i].discriminator_variables,
          global_step=discriminator_global_step,
          update_ops=dis_update_ops[i],
          **kwargs)
      disc_train_ops.append(current_disc_train_op)

    return DiscriminatorTrainOps(disc_train_ops)

  """#### Instantiate train ops"""

  with tf.name_scope(gan_models[-1].generator_scope.original_name_scope):
    if FLAGS.do_lr_decay:
      generator_lr = tf.train.exponential_decay(
        learning_rate=FLAGS.generator_lr,
        global_step=tf.train.get_or_create_global_step(),
        decay_steps=FLAGS.decay_steps,
        decay_rate=FLAGS.decay_rate,
        staircase=True)
    else:
      generator_lr = FLAGS.generator_lr

  def _optimizer(gen_lr, dis_lr):
    kwargs = {'beta1': 0.5, 'beta2': 0.999}
    generator_opt = tf.train.AdamOptimizer(gen_lr, **kwargs)  # **kwargs
    discriminator_opt = tf.train.AdamOptimizer(dis_lr, **kwargs)  # **kwargs
    return generator_opt, discriminator_opt

  gen_opt, dis_opt = _optimizer(generator_lr, FLAGS.discriminator_lr)

  gen_train_op = generator_train_op(
    gan_models[-1],
    gen_loss_tuple,
    gen_opt,
    summarize_gradients=True,
    colocate_gradients_with_ops=True,
    aggregation_method=tf.AggregationMethod.EXPERIMENTAL_ACCUMULATE_N)
  disc_train_ops = discriminator_train_ops(
    gan_models,
    dis_losses,
    dis_opt,
    summarize_gradients=True,
    colocate_gradients_with_ops=True,
    aggregation_method=tf.AggregationMethod.EXPERIMENTAL_ACCUMULATE_N,
    #          transform_grads_fn=tf.contrib.training.clip_gradient_norms_fn(1e3)
  )  ##
  # Create global step increment op.
  global_step = tf.train.get_or_create_global_step()
  global_step_inc_op = global_step.assign_add(1)

  train_ops = tfgan.GANTrainOps(gen_train_op, disc_train_ops, global_step_inc_op)
  tf.summary.scalar('generator_lr', generator_lr)
  tf.summary.scalar('discriminator_lr', FLAGS.discriminator_lr)

  """### Train hooks"""

  def get_sequential_train_hooks(train_steps=tfgan.GANTrainSteps(1, 1)):
    """Returns a hooks function for sequential GAN training.
    Args:
      train_steps: A `GANTrainSteps` tuple that determines how many generator
        and discriminator training steps to take.
    Returns:
      A function that takes a GANTrainOps tuple and returns a list of hooks.
    """

    def get_hooks(train_ops):
      # train_ops: GANTrainOps TUPLE WITH ONE GEN TRAIN OP + LIST OF DIS TRAIN OPS
      hooks = []
      for train_op in train_ops.discriminator_train_op:
        current_discriminator_hook = RunTrainOpsHook(
          train_op,
          train_steps.discriminator_train_steps)
        hooks.append(current_discriminator_hook)

      generator_hook = RunTrainOpsHook(train_ops.generator_train_op,
                                       train_steps.generator_train_steps)
      hooks.append(generator_hook)

      return hooks

    return get_hooks

  """### Actual GAN training"""

  # Run the alternating training loop.
  status_message = tf.string_join(
    ['Starting train step: ',
     tf.as_string(tf.train.get_or_create_global_step())],
    name='status_message')
  # if FLAGS.max_number_of_steps == 0: return
  tfgan.gan_train(
    train_ops,
    FLAGS.train_log_dir,
    get_hooks_fn=get_sequential_train_hooks(),
    hooks=[tf.train.StopAtStepHook(num_steps=FLAGS.max_number_of_steps),
           tf.train.LoggingTensorHook([status_message], every_n_iter=1000)],
    save_summaries_steps=100
    #    master=FLAGS.master,
    #    is_chief=FLAGS.task == 0
  )


if __name__ == '__main__':
  tf.logging.set_verbosity(tf.logging.INFO)
  tf.app.run()
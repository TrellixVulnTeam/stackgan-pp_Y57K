# Copyright 2017 The TensorFlow Authors. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ==============================================================================
"""A TFGAN-backed GAN Estimator."""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import functools
import enum
from contextlib import ExitStack

from tensorflow.contrib.distribute.python import values
from tensorflow.contrib.framework.python.ops import variables as variable_lib
from tensorflow.python.estimator.estimator import create_per_tower_ready_op, \
    create_per_tower_ready_for_local_init_op

import networks
from tfstackgan.python import namedtuples as tfstackgan_tuples
from tfstackgan.python import train as tfstackgan_train
from tfstackgan.python.estimator.python import head_impl as head_lib
from tensorflow.contrib.gan.python.eval.python import \
    summaries as tfgan_summaries
from tensorflow.contrib.gan.python.train import _convert_tensor_or_l_or_d
from tensorflow.python.estimator import estimator
from tensorflow.python.estimator import model_fn as model_fn_lib
from tensorflow.python.framework import ops
from tensorflow.python.ops import variable_scope, random_ops, state_ops
from tensorflow.python.util import tf_inspect as inspect

from tensorflow.python.estimator import model_fn as model_fn_lib
from tensorflow.python.framework import ops
from tensorflow.python.framework import random_seed
from tensorflow.python.training import distribute as distribute_lib
from tensorflow.python.training import monitored_session
from tensorflow.python.training import training_util

import tensorflow.contrib.gan as tfgan

__all__ = [
    'StackGANEstimator',
    'SummaryType'
]


class SummaryType(enum.IntEnum):
    NONE = 0
    VARIABLES = 1
    IMAGES = 2
    IMAGE_COMPARISON = 3


_summary_type_map = {
    SummaryType.VARIABLES: tfgan_summaries.add_gan_model_summaries,
    SummaryType.IMAGES: tfgan_summaries.add_gan_model_image_summaries,
    SummaryType.IMAGE_COMPARISON: tfgan_summaries.add_image_comparison_summaries,
    # pylint:disable=line-too-long
}


# TODO(joelshor): For now, this only supports 1:1 generator:discriminator
# training sequentially. Find a nice way to expose options to the user without
# exposing internals.
class StackGANEstimator(estimator.Estimator):
    """An estimator for Generative Adversarial Networks (GANs).
  
    This Estimator is backed by TFGAN. The network functions follow the TFGAN API
    except for one exception: if either `generator_fn` or `discriminator_fn` have
    an argument called `mode`, then the tf.Estimator mode is passed in for that
    argument. This helps with operations like batch normalization, which have
    different train and evaluation behavior.
  
    Example:
  
    ```python
        import tensorflow as tf
        tfgan = tf.contrib.gan
  
        # See TFGAN's `train.py` for a description of the generator and
        # discriminator API.
        def generator_fn(generator_inputs):
          ...
          return generated_data
  
        def discriminator_fn(data, conditioning):
          ...
          return logits
  
        # Create GAN estimator.
        gan_estimator = tfgan.estimator.GANEstimator(
            model_dir,
            generator_fn=generator_fn,
            discriminator_fn=discriminator_fn,
            generator_loss_fn=tfgan.losses.wasserstein_generator_loss,
            discriminator_loss_fn=tfgan.losses.wasserstein_discriminator_loss,
            generator_optimizer=tf.train.AdamOptimizier(0.1, 0.5),
            discriminator_optimizer=tf.train.AdamOptimizier(0.1, 0.5))
  
        # Train estimator.
        gan_estimator.train(train_input_fn, steps)
  
        # Evaluate resulting estimator.
        gan_estimator.evaluate(eval_input_fn)
  
        # Generate samples from generator.
        predictions = np.array([
            x for x in gan_estimator.predict(predict_input_fn)])
    ```
    """

    def __init__(self,
                 model_dir=None,
                 stack_depth=0,
                 noise_dim=0,
                 batch_size=0,
                 generator_fn=None,
                 discriminator_fn=None,
                 generator_loss_fn=None,
                 apply_batch_norm=False,
                 discriminator_loss_fn=None,
                 generator_optimizer=None,
                 discriminator_optimizer=None,
                 get_hooks_fn=None,
                 # Loss specific
                 uncond_loss_coeff=0.0,
                 color_loss_weight=0.0,
                 gradient_penalty_weight=0.0,
                 # Eval,
                 num_inception_images=0,
                 add_summaries=None,
                 use_loss_summaries=True,
                 config=None):
        """Initializes a GANEstimator instance.
    
        Args:
          model_dir: Directory to save model parameters, graph and etc. This can
            also be used to load checkpoints from the directory into a estimator
            to continue training a previously saved model.
          generator_fn: A python function that takes a Tensor, Tensor list, or
            Tensor dictionary as inputs and returns the outputs of the GAN
            generator. See `TFGAN` for more details and examples. Additionally, if
            it has an argument called `mode`, the Estimator's `mode` will be passed
            in (ex TRAIN, EVAL, PREDICT). This is useful for things like batch
            normalization.
          discriminator_fn: A python function that takes the output of
            `generator_fn` or real data in the GAN setup, and `generator_inputs`.
            Outputs a Tensor in the range [-inf, inf]. See `TFGAN` for more details
            and examples.
          generator_loss_fn: The loss function on the generator. Takes a `GANModel`
            tuple.
          discriminator_loss_fn: The loss function on the discriminator. Takes a
            `GANModel` tuple.
          generator_optimizer: The optimizer for generator updates, or a function
            that takes no arguments and returns an optimizer. This function will
            be called when the default graph is the `GANEstimator`'s graph, so
            utilities like `tf.contrib.framework.get_or_create_global_step` will
            work.
          discriminator_optimizer: Same as `generator_optimizer`, but for the
            discriminator updates.
          get_hooks_fn: A function that takes a `GANTrainOps` tuple and returns a
            list of hooks. These hooks are run on the generator and discriminator
            train ops, and can be used to implement the GAN training scheme.
            Defaults to `train.get_sequential_train_hooks()`.
          add_summaries: `None`, a single `SummaryType`, or a list of `SummaryType`.
          use_loss_summaries: If `True`, add loss summaries. If `False`, does not.
            If `None`, uses defaults.
          config: `RunConfig` object to configure the runtime settings.
        """

        # TODO(joelshor): Explicitly validate inputs.

        # gopt = (generator_optimizer() if callable(generator_optimizer) else
        #         generator_optimizer)
        # dopt = (
        #     discriminator_optimizer() if callable(discriminator_optimizer)
        #     else discriminator_optimizer)

        def _model_fn(features, labels, mode):
            gopt = (generator_optimizer() if callable(generator_optimizer) else
                    generator_optimizer)
            dopt = (
                discriminator_optimizer() if callable(discriminator_optimizer)
                else discriminator_optimizer)
            gan_head = head_lib.stackgan_head(
                generator_loss_fn, discriminator_loss_fn, gopt, dopt,
                uncond_loss_coeff, color_loss_weight, gradient_penalty_weight,
                use_loss_summaries, batch_size, num_inception_images,
                get_hooks_fn=get_hooks_fn)

            return _gan_model_fn(
                stack_depth, batch_size, noise_dim, features, labels, mode,
                generator_fn, discriminator_fn, gan_head,
                add_summaries, apply_batch_norm=apply_batch_norm,
                model_dir=model_dir)

        super(StackGANEstimator, self).__init__(
            model_fn=_model_fn, model_dir=model_dir, config=config)


def _gan_model_fn(
        stack_depth,
        batch_size,
        noise_dim,
        features,
        labels,
        mode,
        generator_fn,
        discriminator_fn,
        head,
        add_summaries=None,
        generator_scope_name='',
        apply_batch_norm=False,
        model_dir=None):
    """The `model_fn` for the GAN estimator.
  
    We make the following convention:
      features -> TFGAN's `generator_inputs`
      labels -> TFGAN's `real_data`
  
    Args:
      features: A dictionary to feed to generator. In the unconditional case,
        this might be just `noise`. In the conditional GAN case, this
        might be the generator's conditioning. The `generator_fn` determines
        what the required keys are.
      labels: Real data. Can be any structure, as long as `discriminator_fn`
        can accept it for the first argument.
      mode: Defines whether this is training, evaluation or prediction.
        See `ModeKeys`.
      generator_fn: A python lambda that takes `generator_inputs` as inputs and
        returns the outputs of the GAN generator.
      discriminator_fn: A python lambda that takes `real_data`/`generated data`
        and `generator_inputs`. Outputs a Tensor in the range [-inf, inf].
      head: A `Head` instance suitable for GANs.
      add_summaries: `None`, a single `SummaryType`, or a list of `SummaryType`.
      generator_scope_name: The name of the generator scope. We need this to be
        the same for GANModels produced by TFGAN's `train.gan_model` and the
        manually constructed ones for predictions.
  
    Returns:
      `ModelFnOps`
  
    Raises:
      ValueError: If `labels` isn't `None` during prediction.
    """
    discriminator_inputs = labels
    generator_inputs = features

    def _get_or_create_gen_super_scope(super_scope):
        if not super_scope:
            with ExitStack() as stack:
                super_scope = stack.enter_context(
                    variable_scope.variable_scope(
                        'Generator', reuse=variable_scope.AUTO_REUSE))
        return super_scope

    generator_scope_name = _get_or_create_gen_super_scope(generator_scope_name)

    if mode == model_fn_lib.ModeKeys.TRAIN:
        gan_models = _make_train_gan_models(
            stack_depth, batch_size, noise_dim, generator_fn, discriminator_fn,
            discriminator_inputs,
            generator_inputs, generator_scope_name, apply_batch_norm,
            add_summaries)
    elif mode == model_fn_lib.ModeKeys.EVAL:
        gan_models = _make_eval_gan_models(
            stack_depth, batch_size, noise_dim, generator_fn, discriminator_fn,
            discriminator_inputs,
            generator_inputs, generator_scope_name, apply_batch_norm,
            add_summaries)
    else:
        if discriminator_inputs is not None:
            raise ValueError('`labels` must be `None` when mode is `predict`. '
                             'Instead, found %s' % discriminator_inputs)
        gan_model = _make_prediction_gan_model(
            stack_depth, batch_size, noise_dim, generator_fn, generator_inputs,
            generator_scope_name,
            apply_batch_norm)
        gan_models = gan_model  # only single model returned, but `logits` is assigned `gan_models`

    return head.create_estimator_spec(features=None, mode=mode,
                                      logits=gan_models, labels=None,
                                      model_dir=model_dir)


def _make_gan_models(stack_depth, batch_size, noise_dim, generator_fn,
                     discriminator_fn,
                     discriminator_inputs, generator_inputs, generator_scope,
                     apply_batch_norm, add_summaries, mode):
    """Make a `GANModel`, and optionally pass in `mode`."""
    # If network functions have an argument `mode`, pass mode to it.
    if 'mode' in inspect.getargspec(generator_fn).args:
        generator_fn = functools.partial(generator_fn, mode=mode)
    if 'mode' in inspect.getargspec(discriminator_fn).args:
        discriminator_fn = functools.partial(discriminator_fn, mode=mode)

    # Sample noise distribution
    noise = random_ops.random_normal([batch_size, noise_dim])
    text_embedding = generator_inputs

    # Conditioning augmentation.
    augmented_conditioning, mu, logvar = networks.augment(text_embedding)

    def _get_generator_input_for_stage(models, stage, noise_sample,
                                       conditioning):
        assert isinstance(stage, int)

        def get_input():
            is_init_stage = not bool(stage)
            # Input into first stage is z ~ p_noise + conditioning.
            # Input for stage i generator is the hidden code outputted by
            # stage (i-1) + conditioning.
            noise = noise_sample if is_init_stage else models[
                stage - 1].generator_hidden_code
            return is_init_stage, noise, conditioning

        return get_input

    # Instantiate GANModel tuples.
    gan_models = []
    for stage in range(stack_depth):
        kwargs = {
            # First element in discriminator_inputs tuple is a list of real data for each stage
            'discriminator_inputs':
            # no list returned by dataset iterator if stack_depth == 1
                (discriminator_inputs, mu) if stack_depth == 1
                else (discriminator_inputs[stage], mu),
            'generator_inputs': _get_generator_input_for_stage(
                gan_models,
                stage,
                noise,  # noise
                augmented_conditioning),  # augmented conditioning
            'stage': stage,
            'generator_super_scope': gan_models[
                -1].generator_scope if stage > 0 else None,
            'mu': mu,
            'logvar': logvar,
            'apply_batch_norm': apply_batch_norm,
            'check_shapes': False}
        current_model = tfstackgan_train.gan_model(
            generator_fn,
            discriminator_fn,
            **kwargs)
        gan_models.append(current_model)

        if add_summaries:
            with ops.device('/cpu:0'):
                if not isinstance(add_summaries, (tuple, list)):
                    add_summaries = [add_summaries]
                with ops.name_scope(None):
                    for summary_type in add_summaries:
                        _summary_type_map[summary_type](current_model)

    return gan_models


def _make_train_gan_models(stack_depth, batch_size, noise_dim, generator_fn,
                           discriminator_fn,
                           discriminator_inputs, generator_inputs,
                           generator_scope, apply_batch_norm, add_summaries):
    """Make a `GANModel` for training."""
    return _make_gan_models(stack_depth, batch_size, noise_dim, generator_fn,
                            discriminator_fn,
                            discriminator_inputs, generator_inputs,
                            generator_scope, apply_batch_norm, add_summaries,
                            model_fn_lib.ModeKeys.TRAIN)


def _make_eval_gan_models(stack_depth, batch_size, noise_dim, generator_fn,
                          discriminator_fn,
                          discriminator_inputs, generator_inputs,
                          generator_scope, apply_batch_norm, add_summaries):
    """Make a `GANModel` for evaluation."""
    return _make_gan_models(stack_depth, batch_size, noise_dim, generator_fn,
                            discriminator_fn,
                            discriminator_inputs, generator_inputs,
                            generator_scope, apply_batch_norm, add_summaries,
                            model_fn_lib.ModeKeys.EVAL)


def _make_prediction_gan_model(stack_depth, batch_size, noise_dim, generator_fn,
                               generator_inputs,
                               generator_scope, apply_batch_norm):
    """Make a `GANModel` from just the generator."""
    # If `generator_fn` has an argument `mode`, pass mode to it.
    if 'mode' in inspect.getargspec(generator_fn).args:
        generator_fn = functools.partial(generator_fn,
                                         mode=model_fn_lib.ModeKeys.PREDICT)

    # Instantiate GANModel tuples.
    def _get_generator_input_for_stage(models, stage, noise_sample,
                                       conditioning):
        assert isinstance(stage, int)

        def get_input():
            is_init_stage = not bool(stage)
            # Input into first stage is z ~ p_noise + conditioning.
            # Input for stage i generator is the hidden code outputted by
            # stage (i-1) + conditioning.
            noise = noise_sample if is_init_stage else models[
                stage - 1].generator_hidden_code
            return is_init_stage, noise, conditioning

        return get_input

    def _get_or_create_gen_super_scope(super_scope):
        if not super_scope:
            with ExitStack() as stack:
                super_scope = stack.enter_context(
                    variable_scope.variable_scope(
                        'Generator', reuse=variable_scope.AUTO_REUSE))
        return super_scope

    text_embedding = generator_inputs

    # Conditioning augmentation.
    augmented_conditioning, mu, logvar = networks.augment(text_embedding)

    # Sample noise distribution
    noise = random_ops.random_normal([batch_size, noise_dim])

    gan_models = []
    for stage in range(stack_depth):
        current_stage_generator_scope = 'Generator_stage_' + str(stage)

        # Wrap generator in super scope.
        generator_super_scope = gan_models[
            -1].generator_scope if stage > 0 else None
        generator_super_scope = _get_or_create_gen_super_scope(
            generator_super_scope)
        with variable_scope.variable_scope(generator_super_scope):
            with ops.name_scope(generator_super_scope.original_name_scope):
                with variable_scope.variable_scope(
                        current_stage_generator_scope,
                        reuse=variable_scope.AUTO_REUSE) as current_gen_scope:
                    print(variable_scope.get_variable_scope().name)
                    # Nested scope, specific to this generator stage.
                    is_init_stage, noise, conditioning = _get_generator_input_for_stage(
                        gan_models,
                        stage,
                        noise,  # noise
                        augmented_conditioning)  # text embedding
                    generator_inputs = _convert_tensor_or_l_or_d(
                        (noise, conditioning))
                    generator_inputs = [is_init_stage] + generator_inputs
                    kwargs = {'final_size': 2 ** (6 + stage),
                              'apply_batch_norm': apply_batch_norm}
                    generated_data, generator_hidden_code = generator_fn(
                        generator_inputs, **kwargs)

        # Get model-specific variables.
        generator_variables = variable_lib.get_trainable_variables(
            generator_super_scope.name)

        current_model = tfstackgan_tuples.StackGANModel(
            generator_inputs,
            generated_data,
            generator_variables,
            generator_super_scope,
            generator_fn,
            generator_hidden_code=generator_hidden_code,
            stage=stack_depth,
            real_data=None,
            discriminator_real_outputs=None,
            discriminator_gen_outputs=None,
            discriminator_variables=None,
            discriminator_scope=None,
            discriminator_fn=None,
            disc_real_outputs_uncond=None,
            disc_gen_outputs_uncond=None,
            mu=mu,
            logvar=logvar)

        gan_models.append(current_model)

    return gan_models[-1]

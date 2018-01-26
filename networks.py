import tensorflow as tf

slim = tf.contrib.slim


def log(x):
  x = tf.cast(x, dtype=tf.float32)
  numerator = tf.log(x)
  denominator = tf.log(2.)
  with tf.Session():
    result = numerator / denominator
    result = result.eval()
  return result


# TODO(joelshor): Use fused batch norm by default. Investigate why some GAN
# setups need the gradient of gradient FusedBatchNormGrad.
def dcgan_generator(inputs,
                    depth=64,
                    final_size=32,
                    num_outputs=3,
                    is_training=True,
                    reuse=None,
                    scope='DCGANGenerator',
                    fused_batch_norm=False):
  """Generator network for DCGAN.
  Construct generator network from inputs to the final endpoint.
  Args:
    inputs: A tensor with any size N. [batch_size, N]
    depth: Number of channels in last deconvolution layer.
    final_size: The shape of the final output.
    num_outputs: Number of output features. For images, this is the number of
      channels.
    is_training: whether is training or not.
    reuse: Whether or not the network has its variables should be reused. scope
      must be given to be reused.
    scope: Optional variable_scope.
    fused_batch_norm: If `True`, use a faster, fused implementation of
      batch norm.
  Returns:
    logits: the pre-softmax activations, a tensor of size
      [batch_size, 32, 32, channels]
    end_points: a dictionary from components of the network to their activation.
  Raises:
    ValueError: If `inputs` is not 2-dimensional.
    ValueError: If `final_size` isn't a power of 2 or is less than 8.
  """
  normalizer_fn = slim.batch_norm
  normalizer_fn_args = {
    'is_training': is_training,
    'zero_debias_moving_mean': True,
    'fused': fused_batch_norm,
  }
  #  inputs.get_shape().assert_has_rank(2)
  if log(final_size) != int(log(final_size)):
    raise ValueError('`final_size` (%i) must be a power of 2.' % final_size)
  if final_size < 8:
    raise ValueError('`final_size` (%i) must be greater than 8.' % final_size)

  end_points = {}
  with tf.variable_scope(scope, values=[inputs], reuse=reuse) as scope:
    with slim.arg_scope([normalizer_fn], **normalizer_fn_args):
      with slim.arg_scope([slim.conv2d_transpose],
                          normalizer_fn=normalizer_fn,
                          stride=2,
                          kernel_size=4):
        if len(inputs.get_shape()) == 2:
          # Init stage.
          num_layers = int(log(final_size)) - 1
          net = tf.expand_dims(tf.expand_dims(inputs, 1), 1)
        else:
          # Next stage.
          num_layers = int(log(final_size)) - (int(log(inputs.shape[1])) - 1) - 1
          net = inputs

        if num_layers > 1:
          # First upscaling is different because it takes the input vector.
          current_depth = depth * 2 ** (num_layers - 1)
          scope = 'deconv1'
          # ! Default activation for slim.conv2d_transpose is relu.
          net = slim.conv2d_transpose(
            net, current_depth, stride=1, padding='VALID', scope=scope)
          end_points[scope] = net

        for i in range(2, num_layers):
          scope = 'deconv%i' % (i)
          current_depth = depth * 2 ** (num_layers - i)
          net = slim.conv2d_transpose(net, current_depth, scope=scope)
          end_points[scope] = net

        # Last layer has different normalizer and activation.
        scope = 'deconv%i' % (num_layers)
        net = slim.conv2d_transpose(
          net, depth, normalizer_fn=None, activation_fn=None, scope=scope)
        end_points[scope] = net

        # Convert to proper channels.
        scope = 'logits'
        logits = slim.conv2d(
          net,
          num_outputs,
          normalizer_fn=None,
          activation_fn=None,
          kernel_size=1,
          stride=1,
          padding='VALID',
          scope=scope)
        end_points[scope] = logits

        logits.get_shape().assert_has_rank(4)
        logits.get_shape().assert_is_compatible_with(
          [None, final_size, final_size, num_outputs])

        return logits, end_points


def generator(inputs, final_size=32, apply_batch_norm=False):
  """Generator to produce CIFAR images.
  Args:
    noise: A 2D Tensor of shape [batch size, noise dim]. Since this example
      does not use conditioning, this Tensor represents a noise vector of some
      kind that will be reshaped by the generator into CIFAR examples.
  Returns:
    A single Tensor with a batch of generated CIFAR images.
  """
  if isinstance(inputs, list):
    # Next stage.
    hidden_code, noise = inputs

    h_code_depth = hidden_code.get_shape()[-1]
    h_code_final_size = hidden_code.get_shape()[1]  # same as hidden_code.get_shape()[2]
    noise = tf.expand_dims(tf.expand_dims(noise, 1), 1)
    noise = tf.tile(noise, [1, h_code_final_size, h_code_final_size, 1])

    noise = tf.concat([noise, hidden_code], -1)

    num_layers = int(log(final_size)) - (int(log(noise.shape[1])) - 1) - 1
  else:
    # Init stage.
    noise = inputs
    num_layers = int(log(final_size)) - 1

  images, end_points = dcgan_generator(
    noise, final_size=final_size, is_training=apply_batch_norm)  # scope=stage_scope, reuse=reuse

  hidden_code = end_points['deconv%i' % (num_layers)]

  # Make sure output lies between [-1, 1].
  return tf.tanh(images), hidden_code


def _validate_image_inputs(inputs):
  inputs.get_shape().assert_has_rank(4)
  inputs.get_shape()[1:3].assert_is_fully_defined()
  if inputs.get_shape()[1] != inputs.get_shape()[2]:
    raise ValueError('Input tensor does not have equal width and height: ',
                     inputs.get_shape()[1:3])
  width = inputs.get_shape().as_list()[1]
  if log(width) != int(log(width)):
    raise ValueError('Input tensor `width` is not a power of 2: ', width)


# TODO(joelshor): Use fused batch norm by default. Investigate why some GAN
# setups need the gradient of gradient FusedBatchNormGrad.
def dcgan_discriminator(inputs,
                        depth=64,
                        is_training=True,
                        reuse=None,
                        scope='DCGANDiscriminator',
                        fused_batch_norm=False):
  """Discriminator network for DCGAN.
  Construct discriminator network from inputs to the final endpoint.
  Args:
    inputs: A tensor of size [batch_size, height, width, channels]. Must be
      floating point.
    depth: Number of channels in first convolution layer.
    is_training: Whether the network is for training or not.
    reuse: Whether or not the network variables should be reused. `scope`
      must be given to be reused.
    scope: Optional variable_scope.
    fused_batch_norm: If `True`, use a faster, fused implementation of
      batch norm.
  Returns:
    logits: The pre-softmax activations, a tensor of size [batch_size, 1]
    end_points: a dictionary from components of the network to their activation.
  Raises:
    ValueError: If the input image shape is not 4-dimensional, if the spatial
      dimensions aren't defined at graph construction time, if the spatial
      dimensions aren't square, or if the spatial dimensions aren't a power of
      two.
  """
  normalizer_fn = slim.batch_norm
  normalizer_fn_args = {
    'is_training': is_training,
    'zero_debias_moving_mean': True,
    'fused': fused_batch_norm,
  }

  _validate_image_inputs(inputs)
  inp_shape = inputs.get_shape().as_list()[1]

  end_points = {}
  with tf.variable_scope(scope, values=[inputs], reuse=reuse) as scope:
    with slim.arg_scope([normalizer_fn], **normalizer_fn_args):
      with slim.arg_scope([slim.conv2d],
                          stride=2,
                          kernel_size=4,
                          activation_fn=tf.nn.leaky_relu):
        net = inputs
        for i in range(int(log(inp_shape))):
          scope = 'conv%i' % (i + 1)
          current_depth = depth * 2 ** i
          normalizer_fn_ = None if i == 0 else normalizer_fn
          net = slim.conv2d(
            net, current_depth, normalizer_fn=normalizer_fn_, scope=scope)
          end_points[scope] = net

        logits = slim.conv2d(net, 1, kernel_size=1, stride=1, padding='VALID',
                             normalizer_fn=None, activation_fn=None)
        logits = tf.reshape(logits, [-1, 1])
        end_points['logits'] = logits

        return logits, end_points


def discriminator(img, unused_conditioning, apply_batch_norm=False):
  """Discriminator for CIFAR images.
  Args:
    img: A Tensor of shape [batch size, width, height, channels], that can be
      either real or generated. It is the discriminator's goal to distinguish
      between the two.
    unused_conditioning: The TFGAN API can help with conditional GANs, which
      would require extra `condition` information to both the generator and the
      discriminator. Since this example is not conditional, we do not use this
      argument.
  Returns:
    A 1D Tensor of shape [batch size] representing the confidence that the
    images are real. The output can lie in [-inf, inf], with positive values
    indicating high confidence that the images are real.
  """
  logits, _ = dcgan_discriminator(img, is_training=apply_batch_norm)
  return logits
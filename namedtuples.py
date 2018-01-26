"""We need to define a custom GAN model to introduce the **Generator superscope**. We use a modified GANModel (StackGANModel) to define every (generator, discriminator) stage. All discriminators are trained independently, but we need the superscope to be able to access the variables of all generators during training.

We also need an extra `hidden_code` property, returning the output of the last generator layer (before conv block), which we query as input to the generator of the next stage in the stack. The `generated_data` will return an actual sample of the generator distribution.

#### Design StackGANModel tuple
"""
from collections import namedtuple

import tensorflow as tf

tfgan = tf.contrib.gan


class StackGANModel(
  namedtuple('StackGANModel', tfgan.GANModel._fields + (
      'generator_hidden_code',
      'stage'
  ))):
  '''Modified GANModel namedtuple for StackGAN architecture.

  Represents a (generator, discriminator) pair at one particular stage.

  Introduces an extra `hidden_code` property, for returning 
  the output of the last generator layer (before conv block), 
  which we query as input to the generator of the next stage in the stack. 
  The `generated_data` will return 
  an actual sample of the generator distribution at this stage.'''


# Maybe customize gan_loss to only compute stage i discriminator loss
# => dis_loss tuple?
# Then define seperate gen_loss tuple that will be used to optimize the whole
# generator net.
class DiscriminatorLoss(
  namedtuple('DiscriminatorLoss', tuple(field
                                        for field
                                        in tfgan.GANLoss._fields
                                        if field != 'generator_loss'))):
  pass


class GeneratorLoss(
  namedtuple('GeneratorLoss', tuple(field
                                    for field
                                    in tfgan.GANLoss._fields
                                    if field != 'discriminator_loss'))):
  pass


class DiscriminatorTrainOps(
  namedtuple('DiscriminatorTrainOps', tuple(field
                                            for field
                                            in tfgan.GANTrainOps._fields
                                            if field not in (
                                                'generator_train_op', 'global_step_inc_op')))):
  pass


class GeneratorTrainOp(
  namedtuple('GeneratorTrainOp', tuple(field
                                       for field
                                       in tfgan.GANTrainOps._fields
                                       if field not in (
                                           'discriminator_train_op', 'global_step_inc_op')))):
  pass
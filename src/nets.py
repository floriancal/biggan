from __future__ import absolute_import
from __future__ import print_function
from __future__ import division

import tensorflow as tf
import functools as ft

from tensorflow.keras.backend import int_shape
from tensorflow.keras.layers import AveragePooling2D
from tensorflow.keras.layers import UpSampling2D
from tensorflow.keras.layers import Concatenate
from tensorflow.keras.layers import Activation
from tensorflow.keras.layers import Embedding
from tensorflow.keras.layers import Reshape
from tensorflow.keras.layers import Lambda
from tensorflow.keras.layers import Add
from tensorflow.keras.layers import Dot
from tensorflow.keras import Model
from tensorflow.keras import Input

from .custom_layers import HyperBatchNorm
from .custom_layers import SpectralConv2D
from .custom_layers import SpectralDense


def module(function):
  '''Successive calls to `function` will be
  variable-scoped with non-conflicting names
  based on `function.__name__`
  '''
  @ft.wraps(function)
  def decorated(*args, **kwargs):
    with tf.compat.v1.variable_scope(None,
        default_name=function.__name__):
      return function(*args, **kwargs)
  return decorated

def TakeChannels(output_dim):
  '''Layer that slices the first `output_dim`
  channels of a given tensor
  '''
  def call(x):
    return x[..., :output_dim]
  def output_shape(input_shape):
    return input_shape[:-1] + (output_dim,)
  return Lambda(call, output_shape=output_shape)

def GlobalSumPooling2D():
  '''Layer that sums over all spatial locations,
  preserving batch and channels dimensions
  '''
  def call(x):
    return tf.reduce_sum(x, axis=(1, 2))
  def output_shape(input_shape):
    return input_shape[0], input_shape[-1]
  return Lambda(call, output_shape=output_shape)

def Conv2D(
    filters,
    kernel_size,
    use_bias=True,
    kernel_initializer='orthogonal',
    padding='same',
  ):
  '''Spectrally-normalized Conv2D layer with
  orthogonal initialization and "same" padding
  '''
  return SpectralConv2D(
    filters=filters,
    kernel_size=kernel_size,
    kernel_initializer=kernel_initializer,
    use_bias=use_bias,
    padding=padding)

def Dense(
    units,
    use_bias=True,
    kernel_initializer='orthogonal',
  ):
  '''Spectrally-normalized Dense layer with
  orthogonal initialization
  '''
  return SpectralDense(
    units=units,
    use_bias=use_bias,
    kernel_initializer=kernel_initializer)

@module
def GBlock(x, z, output_dim, up=False):
  '''Constructs and calls a bottlenecked residual block
  with z-modulated batch normalization and optional
  upsampling for biggan-deep's generator function, G.

  see https://arxiv.org/pdf/1809.11096.pdf,
  figure 16, left side
  '''
  input_dim = int_shape(x)[-1]
  x0 = x
  x = HyperBatchNorm()([x, z])
  x = Activation('relu')(x)
  x = Conv2D(input_dim // 4, 1, use_bias=False)(x)
  x = HyperBatchNorm()([x, z])
  x = Activation('relu')(x)
  if up:
    x = UpSampling2D()(x)
  x = Conv2D(input_dim // 4, 3, use_bias=False)(x)
  x = HyperBatchNorm()([x, z])
  x = Activation('relu')(x)
  x = Conv2D(input_dim // 4, 3, use_bias=False)(x)
  x = HyperBatchNorm()([x, z])
  x = Activation('relu')(x)
  x = Conv2D(output_dim, 1, use_bias=False)(x)
  if input_dim > output_dim:
    x0 = TakeChannels(output_dim)(x0)
  elif input_dim < output_dim:
    raise ValueError
  if up:
    x0 = UpSampling2D()(x0)
  return Add()([x, x0])

@module
def DBlock(x, output_dim, down=False):
  '''Constructs a bottlenecked residual block
  with optional average pooling for biggan-deep's
  discriminator function, D.

  see https://arxiv.org/pdf/1809.11096.pdf,
  figure 16, right side
  '''
  input_dim = int_shape(x)[-1]
  x0 = x
  x = Activation('relu')(x)
  x = Conv2D(output_dim // 4, 1)(x)
  x = Activation('relu')(x)
  x = Conv2D(output_dim // 4, 3)(x)
  x = Activation('relu')(x)
  x = Conv2D(output_dim // 4, 3)(x)
  x = Activation('relu')(x)
  if down:
    x = AveragePooling2D()(x)
    x0 = AveragePooling2D()(x0)
  if input_dim < output_dim:
    extra = output_dim - input_dim
    x0_extra = Conv2D(extra, 1, use_bias=False)(x0)
    x0 = Concatenate()([x0, x0_extra])
  elif input_dim > output_dim:
    raise ValueError
  x = Conv2D(output_dim, 1)(x)
  return Add()([x, x0])

@module
def Attention(x, use_bias=True):
  '''Constructs a self-attention layer

  see https://arxiv.org/pdf/1805.08318.pdf,
  section 3, and the corresponding code:
  https://github.com/brain-research/self-attention-gan
  '''
  batch, height, width, channels = int_shape(x)
  space = height * width
  f = Conv2D(channels // 8, 1, use_bias=False)(x)
  f = Reshape((space, channels // 8))(f)
  g = AveragePooling2D()(x)
  g = Conv2D(channels // 8, 1, use_bias=False)(g)
  g = Reshape((space // 4, channels // 8))(g)
  h = AveragePooling2D()(x)
  h = Conv2D(channels // 2, 1, use_bias=False)(h)
  h = Reshape((space // 4, channels // 2))(h)
  attn = Dot((2, 2))([f, g])
  attn = Activation('softmax')(attn)
  y = Dot((2, 1))([attn, h])
  y = Reshape((height, width, channels // 2))(y)
  y = Conv2D(channels, 1, use_bias=use_bias)(y)
  return Add()([x, y])

def Generator(ch, num_classes=27):
  '''Cf. https://arxiv.org/pdf/1809.11096.pdf,
  table 8, left side
  '''
  # input z-vector
  z = Input((128,))
  
  # input class index
  y = Input(())

  # 128-dimensional class embedding
  y_emb = Embedding(num_classes, 128)(y) 
  
  # concatenate with z
  c = Concatenate()([z, y_emb])

  # project and reshape
  x = Dense(4 * 4 * 16 * ch, use_bias=False)(c)
  x = Reshape((4, 4, 16 * ch))(x)

  # (4, 4, 16ch) -> (8, 8, 16ch)
  x = GBlock(x, c, 16 * ch)
  x = GBlock(x, c, 16 * ch, up=True)

  # (8, 8, 16ch) -> (16, 16, 8ch)
  x = GBlock(x, c, 16 * ch)
  x = GBlock(x, c, 8 * ch, up=True)

  # (16, 16, 8ch) -> (32, 32, 8ch)
  x = GBlock(x, c, 8 * ch)
  x = GBlock(x, c, 8 * ch, up=True)

  # (32, 32, 8ch) -> (64, 64, 4ch)
  x = GBlock(x, c, 8 * ch)
  x = GBlock(x, c, 4 * ch, up=True)

  # non-local @ (64, 64, 4ch)
  x = Attention(x, use_bias=False)

  # (64, 64, 4ch) -> (128, 128, 2ch)
  x = GBlock(x, c, 4 * ch)
  x = GBlock(x, c, 2 * ch, up=True)

  # (128, 128, 2ch) -> (256, 256, 1ch)
  x = GBlock(x, c, 2 * ch)
  x = GBlock(x, c, 1 * ch, up=True)

  # (256, 256, 1ch) -> (256, 256, 3)
  x = HyperBatchNorm()([x, c])
  x = Activation('relu')(x)
  x = Conv2D(3, 3)(x)
  x = Activation('tanh')(x)

  # return keras model
  return Model([z, y], x, name='Generator')

def Discriminator(ch, num_classes=27):
  '''Cf. https://arxiv.org/pdf/1809.11096.pdf,
  table 8, right side
  '''
  y = Input(())
  y_emb = Embedding(num_classes, 16 * ch)(y)

  # (256, 256, 3) -> (256, 256, 1ch)
  x = inp = Input((256, 256, 3))
  x = Conv2D(ch, 3)(x)

  # (256, 256, 1ch) -> (128, 128, 2ch)
  x = DBlock(x, 2 * ch, down=True)
  x = DBlock(x, 2 * ch)

  # (128, 128, 2ch) -> (64, 64, 4ch)
  x = DBlock(x, 4 * ch, down=True)
  x = DBlock(x, 4 * ch)

  # non-local @ (64, 64, 4ch)
  x = Attention(x)

  # (64, 64, 4ch) -> (32, 32, 8ch)
  x = DBlock(x, 8 * ch, down=True)
  x = DBlock(x, 8 * ch)

  # (32, 32, 8ch) -> (16, 16, 8ch)
  x = DBlock(x, 8 * ch, down=True)
  x = DBlock(x, 8 * ch)

  # (16, 16, 8ch) -> (8, 8, 16ch)
  x = DBlock(x, 16 * ch, down=True)
  x = DBlock(x, 16 * ch)

  # (8, 8, 16ch) -> (4, 4, 16ch)
  x = DBlock(x, 16 * ch, down=True)
  x = DBlock(x, 16 * ch)

  # (4, 4, 16ch) -> (16ch,)
  x = Activation('relu')(x)
  x = GlobalSumPooling2D()(x)
  
  # conditional logit
  x = Add()([Dense(1)(x), Dot((1, 1))([x, y_emb])])

  # return keras model
  return Model([inp, y], x, name='Discriminator')

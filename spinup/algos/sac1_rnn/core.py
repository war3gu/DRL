import numpy as np
import tensorflow as tf
from numbers import Number

EPS = 1e-8

def placeholder(dim=None):
    if dim is None:
        return tf.placeholder(dtype=tf.float32, shape=(None,))
    elif isinstance(dim, Number):
        return tf.placeholder(dtype=tf.float32, shape=(None, dim))
    else:
        return tf.placeholder(dtype=tf.float32, shape=((None,) + dim))

def placeholders(*args):
    return [placeholder(dim) for dim in args]

def mlp(x, hidden_sizes=(32,), activation=tf.tanh, output_activation=None):
    for h in hidden_sizes[:-1]:
        x = tf.layers.dense(x, units=h, activation=activation)
    return tf.layers.dense(x, units=hidden_sizes[-1], activation=output_activation)

def get_vars(scope):
    return [x for x in tf.global_variables() if scope in x.name]

def count_vars(scope):
    v = get_vars(scope)
    return sum([np.prod(var.shape.as_list()) for var in v])

def gaussian_likelihood(x, mu, log_std):
    pre_sum = -0.5 * (((x-mu)/(tf.exp(log_std)+EPS))**2 + 2*log_std + np.log(2*np.pi))
    return tf.reduce_sum(pre_sum, axis=1)

def clip_but_pass_gradient(x, l=-1., u=1.):
    clip_up = tf.cast(x > u, tf.float32)
    clip_low = tf.cast(x < l, tf.float32)
    return x + tf.stop_gradient((u - x)*clip_up + (l - x)*clip_low)


"""
Policies
"""

LOG_STD_MAX = 2
LOG_STD_MIN = -20

def mlp_gaussian_policy(x, a, hidden_sizes, activation, output_activation):
    act_dim = a.shape.as_list()[-1]
    net = mlp(x, list(hidden_sizes), activation, activation)
    mu = tf.layers.dense(net, act_dim, activation=output_activation)

    """
    Because algorithm maximizes trade-off of reward and entropy,
    entropy must be unique to state---and therefore log_stds need
    to be a neural network output instead of a shared-across-states
    learnable parameter vector. But for deep Relu and other nets,
    simply sticking an activationless dense layer at the end would
    be quite bad---at the beginning of training, a randomly initialized
    net could produce extremely large values for the log_stds, which
    would result in some actions being either entirely deterministic
    or too random to come back to earth. Either of these introduces
    numerical instability which could break the algorithm. To 
    protect against that, we'll constrain the output range of the 
    log_stds, to lie within [LOG_STD_MIN, LOG_STD_MAX]. This is 
    slightly different from the trick used by the original authors of
    SAC---they used tf.clip_by_value instead of squashing and rescaling.
    I prefer this approach because it allows gradient propagation
    through log_std where clipping wouldn't, but I don't know if
    it makes much of a difference.
    """
    log_std = tf.layers.dense(net, act_dim, activation=tf.tanh)
    log_std = LOG_STD_MIN + 0.5 * (LOG_STD_MAX - LOG_STD_MIN) * (log_std + 1)

    std = tf.exp(log_std)
    pi = mu + tf.random_normal(tf.shape(mu)) * std
    logp_pi = gaussian_likelihood(pi, mu, log_std)
    return mu, pi, logp_pi

def apply_squashing_func(mu, pi, logp_pi):
    mu = tf.tanh(mu)
    pi = tf.tanh(pi)
    # To avoid evil machine precision error, strictly clip 1-pi**2 to [0,1] range.
    logp_pi -= tf.reduce_sum(tf.log(clip_but_pass_gradient(1 - pi**2, l=0, u=1) + 1e-6), axis=1)
    return mu, pi, logp_pi


"""
Actor-Critics
"""
def mlp_actor_critic(x, x2, a, hidden_sizes=(400,300), activation=tf.nn.relu,
                     output_activation=None, policy=mlp_gaussian_policy, action_space=None):
    # policy
    with tf.variable_scope('pi', reuse=tf.AUTO_REUSE):
        mu, pi, logp_pi = policy(x, a, hidden_sizes, activation, output_activation)
        # mu, pi, logp_pi = tf.stop_gradient(policy(x, a, hidden_sizes, activation, output_activation))
        mu, pi, logp_pi = apply_squashing_func(mu, pi, logp_pi)
    with tf.variable_scope('pi', reuse=True):
        mu2, pi2, logp_pi2 = policy(x2, a, hidden_sizes, activation, output_activation)
        mu2, pi2, logp_pi2 = apply_squashing_func(mu2, pi2, logp_pi2)

    # make sure actions are in correct range
    action_scale = action_space.high[0]
    mu *= action_scale
    pi *= action_scale

    # vfs
    # tf.squeeze( shape(?,1), axis=1 ) = shape(?,)
    vf_mlp = lambda x : tf.squeeze(mlp(x, list(hidden_sizes)+[1], activation, None), axis=1)
    with tf.variable_scope('q1', reuse=tf.AUTO_REUSE):
        q1 = vf_mlp(tf.concat([x,a], axis=-1))
    with tf.variable_scope('q1', reuse=True):
        q1_pi = vf_mlp(tf.concat([x,pi], axis=-1))
    with tf.variable_scope('q2', reuse=tf.AUTO_REUSE):
        q2 = vf_mlp(tf.concat([x,a], axis=-1))
    with tf.variable_scope('q2', reuse=True):
        q2_pi = vf_mlp(tf.concat([x,pi], axis=-1))

    return mu, pi, logp_pi, logp_pi2, q1, q2, q1_pi, q2_pi




def sac1_dynamic_rnn(x, hc_0, hc_size=128):
    """
    define GRU cell and run cell on given sequence from s_t_o
    outputs N L H
    states  N   H
    x       N L D
    hc_0    N   H
    """
    hc_size = int(hc_0.shape[1])
    basic_cell = tf.nn.rnn_cell.GRUCell(num_units=hc_size, reuse=tf.AUTO_REUSE)

    outputs, states = tf.nn.dynamic_rnn(basic_cell, x, initial_state=hc_0, dtype=tf.float32)

    return outputs, states   # N T H  N H


def sac1_dynamic_rnn1(x, hc_0, hc_size=128):  # sac1_dynamic_cudnn_rnn
    """
    define cudnn GRU cell and run cell on given sequence from s_t_o
    outputs N T H
    states  N H
    X       N T D
    s_t_0   N H
    """
    with tf.variable_scope("rnn", reuse=tf.AUTO_REUSE):
        basic_cell = tf.contrib.cudnn_rnn.CudnnGRU(num_layers=1, num_units=hc_size)
        # basic_cell = tf.contrib.cudnn_rnn.CudnnGRUSaveable(num_layers=1, num_units=h_size)
        hc_0 = tf.expand_dims(hc_0, 0)
        outputs, states = basic_cell(tf.transpose(x, (1, 0, 2)), initial_state=(hc_0,))   # N T D to T N D
    # print(states[0][0])
    return tf.transpose(outputs, (1, 0, 2)), states[0][0]  # N T H  N H

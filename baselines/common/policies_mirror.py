import tensorflow as tf
from baselines.common import tf_util
from baselines.a2c.utils import fc
from baselines.common.distributions_mirror import make_pdtype
from baselines.common.input import observation_placeholder, encode_observation
from baselines.common.tf_util import adjust_shape
from baselines.common.mpi_running_mean_std import RunningMeanStd
from baselines.common.models import get_network_builder
from baselines.common.mirror_util import mirror_modify
import gym


class PolicyWithValue(object):
    """
    Encapsulates fields and methods for RL policy and value function estimation with shared parameters
    """

    def __init__(self, env, observations, latent, mirrorlatent, estimate_q=False, vf_latent=None, sess=None, env_name=None, **tensors):
        """
        Parameters:
        ----------
        env             RL environment

        observations    tensorflow placeholder in which the observations will be fed

        latent          latent state from which policy distribution parameters should be inferred

        vf_latent       latent state from which value function should be inferred (if None, then latent is used)

        sess            tensorflow session to run calculations in (if None, default session is used)

        **tensors       tensorflow tensors for additional attributes such as state or mask

        """

        self.X = observations
        self.state = tf.constant([])
        self.initial_state = None
        self.__dict__.update(tensors)

        vf_latent = vf_latent if vf_latent is not None else latent
        vf_latent = tf.layers.flatten(vf_latent)
        latent = tf.layers.flatten(latent)
        
        mirror_vf_latent = mirrorlatent
        mirrorlatent = tf.layers.flatten(mirrorlatent)
        mirror_vf_latent = tf.layers.flatten(mirror_vf_latent)
        
        latent_all = tf.concat([latent,mirrorlatent], axis=0)
        
        vf_latent_all = tf.concat([vf_latent,mirror_vf_latent], axis=0)
        # Based on the action space, will select what probability distribution type
        self.pdtype = make_pdtype(env.action_space)
        
        self.pd, self.pi_all = self.pdtype.pdfromlatent(latent_all, init_scale=0.01)
        self.pi = self.pi_all[0:latent.shape[0],:]
        pi_mirror = self.pi_all[latent.shape[0]:,:]
        # Take an action
        self.action = self.pd.sample()
        pi_mirror = mirror_modify(pi_mirror, game=env_name)
        pi_origin = tf.nn.softmax(self.pi)
        pi_mirror = tf.nn.softmax(pi_mirror)
        # policy mirror loss
        self.policy_mirrorloss = tf.reduce_mean(tf.square(pi_origin - pi_mirror), 1)
        # Calculate the neg log of our probability
        self.neglogp = self.pd.neglogp(self.action)
        self.sess = sess or tf.get_default_session()

        if estimate_q:
            assert isinstance(env.action_space, gym.spaces.Discrete)
            self.q = fc(vf_latent, 'q', env.action_space.n)
            self.vf = self.q
        else:
            vf_all = fc(vf_latent_all, 'vf', 1)
            self.vf = vf_all[0:vf_latent.shape[0],:]
            self.vf = self.vf[:,0]
            vf_mirror = vf_all[vf_latent.shape[0]:,:]
            vf_mirror = vf_mirror[:,0]
        # value mirror loss
        self.value_mirrorloss = tf.square(self.vf - vf_mirror)

    def _evaluate(self, variables, observation, **extra_feed):
        sess = self.sess
        feed_dict = {self.X: adjust_shape(self.X, observation)}
        for inpt_name, data in extra_feed.items():
            if inpt_name in self.__dict__.keys():
                inpt = self.__dict__[inpt_name]
                if isinstance(inpt, tf.Tensor) and inpt._op.type == 'Placeholder':
                    feed_dict[inpt] = adjust_shape(inpt, data)

        return sess.run(variables, feed_dict)

    def step(self, observation, **extra_feed):
        """
        Compute next action(s) given the observation(s)

        Parameters:
        ----------

        observation     observation data (either single or a batch)

        **extra_feed    additional data such as state or mask (names of the arguments should match the ones in constructor, see __init__)

        Returns:
        -------
        (action, value estimate, next state, negative log likelihood of the action under current policy parameters) tuple
        """

        a, v, state, neglogp = self._evaluate([self.action, self.vf, self.state, self.neglogp], observation, **extra_feed)
        if state.size == 0:
            state = None
        return a, v, state, neglogp

    def value(self, ob, *args, **kwargs):
        """
        Compute value estimate(s) given the observation(s)

        Parameters:
        ----------

        observation     observation data (either single or a batch)

        **extra_feed    additional data such as state or mask (names of the arguments should match the ones in constructor, see __init__)

        Returns:
        -------
        value estimate
        """
        return self._evaluate(self.vf, ob, *args, **kwargs)

    def save(self, save_path):
        tf_util.save_state(save_path, sess=self.sess)

    def load(self, load_path):
        tf_util.load_state(load_path, sess=self.sess)

def build_mirror_policy(env, policy_network, value_network=None,  normalize_observations=False, estimate_q=False, env_name=None, **policy_kwargs):
    if isinstance(policy_network, str):
        network_type = policy_network
        policy_network = get_network_builder(network_type)(**policy_kwargs)

    def policy_fn(nbatch=None, nsteps=None, sess=None, observ_placeholder=None):
        ob_space = env.observation_space

        X = observ_placeholder if observ_placeholder is not None else observation_placeholder(ob_space, batch_size=nbatch)
        # mirror
        X_mirror = tf.reverse(X, axis=[2])
        extra_tensors = {}
        if normalize_observations and X.dtype == tf.float32:
            encoded_x, rms = _normalize_clip_observation(X)
            
            encoded_x_mirror, _ = _normalize_clip_observation(X_mirror)
            extra_tensors['rms'] = rms
        else:
            encoded_x = X
            encoded_x_mirror = X_mirror

        encoded_x = encode_observation(ob_space, encoded_x)
        encoded_x_mirror = encode_observation(ob_space,encoded_x_mirror)

        with tf.variable_scope('pi', reuse=tf.AUTO_REUSE):
            policy_latent = policy_network(encoded_x)
            
            policy_latent_mirror = policy_network(encoded_x_mirror)
            if isinstance(policy_latent, tuple):
                policy_latent, recurrent_tensors = policy_latent
                policy_latent_mirror, recurrent_tensors_mirror = policy_latent_mirror

                if recurrent_tensors is not None:
                    # recurrent architecture, need a few more steps
                    nenv = nbatch // nsteps
                    assert nenv > 0, 'Bad input for recurrent policy: batch size {} smaller than nsteps {}'.format(nbatch, nsteps)
                    policy_latent, recurrent_tensors = policy_network(encoded_x, nenv)
                    policy_latent_mirror, recurrent_tensors_mirror = policy_network(encoded_x_mirror,nenv)
                    extra_tensors.update(recurrent_tensors)


        _v_net = value_network

        if _v_net is None or _v_net == 'shared':
            vf_latent = policy_latent
        else:
            if _v_net == 'copy':
                _v_net = policy_network
            else:
                assert callable(_v_net)

            with tf.variable_scope('vf', reuse=tf.AUTO_REUSE):
                # TODO recurrent architectures are not supported with value_network=copy yet
                vf_latent = _v_net(encoded_x)

        policy = PolicyWithValue(
            env=env,
            observations=X,
            latent=policy_latent,
            mirrorlatent=policy_latent_mirror,
            vf_latent=vf_latent,
            sess=sess,
            estimate_q=estimate_q,
            env_name=env_name,
            **extra_tensors
        )
        return policy

    return policy_fn


def _normalize_clip_observation(x, clip_range=[-5.0, 5.0]):
    rms = RunningMeanStd(shape=x.shape[1:])
    norm_x = tf.clip_by_value((x - rms.mean) / rms.std, min(clip_range), max(clip_range))
    return norm_x, rms


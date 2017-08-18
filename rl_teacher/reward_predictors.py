import os
import random
from time import sleep

from multiprocessing import Process

import numpy as np
import tensorflow as tf
from keras import backend as K

from rl_teacher.summaries import AgentLogger
from rl_teacher.nn import FullyConnectedMLP, SimpleConvolveObservationQNet
from rl_teacher.segment_sampling import sample_segment_from_path
from rl_teacher.utils import corrcoef

class TraditionalRLRewardPredictor(object):
    """Predictor that always returns the true reward provided by the environment."""

    def __init__(self, summary_writer):
        self.agent_logger = AgentLogger(summary_writer)

    def predict_reward(self, path):
        return path["original_rewards"]

    def path_callback(self, path):
        pass

class ComparisonRewardPredictor(object):
    """Predictor that trains a model to predict how much reward is contained in a trajectory segment"""

    def __init__(self, env, experiment_name, summary_writer, comparison_collector, agent_logger, label_schedule, clip_length, stacked_frames):
        self.summary_writer = summary_writer
        self.agent_logger = agent_logger
        self.comparison_collector = comparison_collector
        self.label_schedule = label_schedule
        self.experiment_name = experiment_name

        # Set up some bookkeeping
        self._frames_per_segment = clip_length * env.fps
        self._steps_since_last_training = 0
        self._steps_since_last_checkpoint = 0
        self._n_timesteps_per_predictor_training = 2e3  # How often should we train our predictor?
        self._n_timesteps_per_checkpoint = 2e4  # How often should we save our model
        self._elapsed_predictor_training_iters = 0
        self._num_checkpoints = 0

        # Build and initialize our predictor model
        config = tf.ConfigProto(
            # device_count={'GPU': 0},
            # log_device_placement=True,
        )
        config.gpu_options.per_process_gpu_memory_fraction = 0.35  # allow_growth = True
        self.sess = tf.Session(config=config)

        self.obs_shape = env.observation_space.shape
        if stacked_frames > 0:
            self.obs_shape = self.obs_shape + (stacked_frames,)
        self.discrete_action_space = not hasattr(env.action_space, "shape")
        self.act_shape = (env.action_space.n,) if self.discrete_action_space else env.action_space.shape

        self.graph = self._build_model()
        self.sess.run(tf.global_variables_initializer())
        my_vars = tf.global_variables()
        self.saver = tf.train.Saver({var.name: var for var in my_vars}, max_to_keep=0)

    def _predict_rewards(self, obs_segments, act_segments, network):
        """
        :param obs_segments: tensor with shape = (batch_size, segment_length) + obs_shape
        :param act_segments: tensor with shape = (batch_size, segment_length) + act_shape
        :param network: neural net with .run() that maps obs and act tensors into a (scalar) value tensor
        :return: tensor with shape = (batch_size, segment_length)
        """
        batchsize = tf.shape(obs_segments)[0]
        segment_length = tf.shape(obs_segments)[1]

        # Temporarily chop up segments into individual observations and actions
        obs = tf.reshape(obs_segments, (-1,) + self.obs_shape)
        acts = tf.reshape(act_segments, (-1,) + self.act_shape)

        # Run them through our neural network
        rewards = network.run(obs, acts)

        # Group the rewards back into their segments
        return tf.reshape(rewards, (batchsize, segment_length))

    def _build_model(self):
        """
        Our model takes in path segments with states and actions, and generates Q values.
        These Q values serve as predictions of the true reward.
        We can compare two segments and sum the Q values to get a prediction of a label
        of which segment is better. We then learn the weights for our model by comparing
        these labels with an authority (either a human or synthetic labeler).
        """
        # Set up observation placeholders
        self.segment_obs_placeholder = tf.placeholder(
            dtype=tf.float32, shape=(None, None) + self.obs_shape, name="obs_placeholder")
        self.segment_alt_obs_placeholder = tf.placeholder(
            dtype=tf.float32, shape=(None, None) + self.obs_shape, name="alt_obs_placeholder")

        # Set up action placeholders
        if self.discrete_action_space:
            self.segment_act_placeholder = tf.placeholder(
                dtype=tf.float32, shape=(None, None), name="act_placeholder")
            self.segment_alt_act_placeholder = tf.placeholder(
                dtype=tf.float32, shape=(None, None), name="alt_act_placeholder")
            # Discrete actions need to become one-hot vectors for the model
            segment_act = tf.one_hot(tf.cast(self.segment_act_placeholder, tf.int32), self.act_shape[0])
            segment_alt_act = tf.one_hot(tf.cast(self.segment_alt_act_placeholder, tf.int32), self.act_shape[0])
        else:
            self.segment_act_placeholder = tf.placeholder(
                dtype=tf.float32, shape=(None, None) + self.act_shape, name="act_placeholder")
            self.segment_alt_act_placeholder = tf.placeholder(
                dtype=tf.float32, shape=(None, None) + self.act_shape, name="alt_act_placeholder")
            # Assume the actions are how we want them
            segment_act = self.segment_act_placeholder
            segement_alt_act = self.segment_alt_act_placeholder

        # A vanilla multi-layer perceptron maps a (state, action) pair to a reward (Q-value)
        # net = FullyConnectedMLP(self.obs_shape, self.act_shape)
        net = SimpleConvolveObservationQNet(self.obs_shape, self.act_shape)

        self.q_value = self._predict_rewards(self.segment_obs_placeholder, segment_act, net)
        alt_q_value = self._predict_rewards(self.segment_alt_obs_placeholder, segment_alt_act, net)

        # We use trajectory segments rather than individual (state, action) pairs because
        # video clips of segments are easier for humans to evaluate
        segment_reward_pred_left = tf.reduce_sum(self.q_value, axis=1)
        segment_reward_pred_right = tf.reduce_sum(alt_q_value, axis=1)
        reward_logits = tf.stack([segment_reward_pred_left, segment_reward_pred_right], axis=1)  # (batch_size, 2)

        self.labels = tf.placeholder(dtype=tf.int32, shape=(None,), name="comparison_labels")

        # delta = 1e-5
        # clipped_comparison_labels = tf.clip_by_value(self.comparison_labels, delta, 1.0-delta)

        data_loss = tf.nn.sparse_softmax_cross_entropy_with_logits(logits=reward_logits, labels=self.labels)

        self.loss_op = tf.reduce_mean(data_loss)

        global_step = tf.Variable(0, name='global_step', trainable=False)
        self.train_op = tf.train.AdamOptimizer().minimize(self.loss_op, global_step=global_step)

        return tf.get_default_graph()

    def predict_reward(self, path):
        """Predict the reward for each step in a given path"""
        with self.graph.as_default():
            q_value = self.sess.run(self.q_value, feed_dict={
                self.segment_obs_placeholder: np.asarray([path["obs"]]),
                self.segment_act_placeholder: np.asarray([path["actions"]]),
                K.learning_phase(): False
            })
        return q_value[0]

    def path_callback(self, path):
        path_length = len(path["obs"])
        self._steps_since_last_training += path_length
        self._steps_since_last_checkpoint += path_length

        # self.agent_logger.log_episode(path)  <-- This is a huge memory problem!

        # We may be in a new part of the environment, so we take new segments to build comparisons from
        # TODO: Reduce the quantity of segments!
        # TODO: Prioritize new segements when doing comparisons!
        segment = sample_segment_from_path(path, int(self._frames_per_segment))
        if segment and len(self.comparison_collector._segments) < 1000:
            self.comparison_collector.add_segment(segment)

        # If we need more comparisons, then we build them from our recent segments
        if len(self.comparison_collector) < self.label_schedule.n_desired_labels:
            self.comparison_collector.invent_comparison()

        # Train our predictor every X steps
        if self._steps_since_last_training >= self._n_timesteps_per_predictor_training:
            self.train_predictor()
            self._steps_since_last_training = 0

        # Save our predictor every X steps
        if self._steps_since_last_checkpoint >= self._n_timesteps_per_checkpoint:
            print("Saving reward model checkpoint!")
            self._num_checkpoints += 1
            self.saver.save(self.sess, self._checkpoint_filename())
            self._steps_since_last_checkpoint = 0

    def _checkpoint_filename(self):
        return 'checkpoints/reward_model/%s/%08d' % (self.experiment_name, self._num_checkpoints)

    def load_model_from_checkpoint(self):
        filename = tf.train.latest_checkpoint(os.path.dirname(self._checkpoint_filename()))
        self.saver.restore(self.sess, filename)

    def train_predictor(self):
        self.comparison_collector.label_unlabeled_comparisons()

        minibatch_size = min(128, len(self.comparison_collector.labeled_decisive_comparisons))
        comparisons = random.sample(self.comparison_collector.labeled_decisive_comparisons, minibatch_size)
        left_segs = [self.comparison_collector.get_segment(comp['left']) for comp in comparisons]
        right_segs = [self.comparison_collector.get_segment(comp['right']) for comp in comparisons]

        left_obs = np.asarray([left['obs'] for left in left_segs])
        left_acts = np.asarray([left['actions'] for left in left_segs])
        right_obs = np.asarray([right['obs'] for right in right_segs])
        right_acts = np.asarray([right['actions'] for right in right_segs])
        labels = np.asarray([comp['label'] for comp in comparisons])

        with self.graph.as_default():
            _, loss = self.sess.run([self.train_op, self.loss_op], feed_dict={
                self.segment_obs_placeholder: left_obs,
                self.segment_act_placeholder: left_acts,
                self.segment_alt_obs_placeholder: right_obs,
                self.segment_alt_act_placeholder: right_acts,
                self.labels: labels,
                K.learning_phase(): True
            })
            self._elapsed_predictor_training_iters += 1
            self._write_training_summaries(loss)

    def _write_training_summaries(self, loss):
        self.agent_logger.log_simple("predictor/loss", loss)

        # Calculate correlation between true and predicted reward by running validation on recent episodes
        recent_paths = self.agent_logger.get_recent_paths_with_padding()
        if len(recent_paths) > 1 and self.agent_logger.summary_step % 10 == 0:  # Run validation every 10 iters
            validation_obs = np.asarray([path["obs"] for path in recent_paths])
            validation_acts = np.asarray([path["actions"] for path in recent_paths])
            q_value = self.sess.run(self.q_value, feed_dict={
                self.segment_obs_placeholder: validation_obs,
                self.segment_act_placeholder: validation_acts,
                K.learning_phase(): False
            })
            ep_reward_pred = np.sum(q_value, axis=1)
            reward_true = np.asarray([path['original_rewards'] for path in recent_paths])
            ep_reward_true = np.sum(reward_true, axis=1)
            self.agent_logger.log_simple("predictor/correlations", corrcoef(ep_reward_true, ep_reward_pred))

        self.agent_logger.log_simple("predictor/num_training_iters", self._elapsed_predictor_training_iters)
        self.agent_logger.log_simple("labels/desired_labels", self.label_schedule.n_desired_labels)
        self.agent_logger.log_simple("labels/total_comparisons", len(self.comparison_collector))
        self.agent_logger.log_simple(
            "labels/labeled_comparisons", len(self.comparison_collector.labeled_decisive_comparisons))

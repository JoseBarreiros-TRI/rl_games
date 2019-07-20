
import networks
import tr_helpers
import experience
import wrappers
import tensorflow as tf
import numpy as np
import collections
import time
from collections import deque, OrderedDict
from tensorboardX import SummaryWriter
from tensorflow_utils import TensorFlowVariables
import gym
import vecenv
from tf_moving_mean_std import MovingMeanStd

def swap_and_flatten01(arr):
    s = arr.shape
    return arr.swapaxes(0, 1).reshape(s[0] * s[1], *s[2:])

#(-1, 1) -> (low, high)
def rescale_actions(low, high, action):
    d = (high - low) / 2.0
    m = (high + low) / 2.0
    scaled_action =  action * d + m
    return scaled_action

#(steps_num, actions_num)
def policy_kl(p0_mu, p0_sigma, p1_mu, p1_sigma):
    c1 = np.log(p0_sigma/p1_sigma + 1e-5)
    c2 = (np.square(p0_sigma) + np.square(p1_mu - p0_mu))/(2.0 *(np.square(p1_sigma) + 1e-5))
    c3 = -1.0 / 2.0
    kl = c1 + c2 + c3
    kl = np.mean(np.sum(kl, axis = -1)) # returning mean between all steps of sum between all actions
    return kl

def policy_kl_tf(p0_mu, p0_sigma, p1_mu, p1_sigma):
    c1 = tf.log(p1_sigma/p0_sigma + 1e-5)
    c2 = (tf.square(p0_sigma) + tf.square(p1_mu - p0_mu))/(2.0 *(tf.square(p1_sigma) + 1e-5))
    c3 = -1.0 / 2.0
    kl = c1 + c2 + c3
    kl = tf.reduce_mean(tf.reduce_sum(kl, axis = -1)) # returning mean between all steps of sum between all actions
    return kl

class A2CAgent:
    def __init__(self, sess, name, observation_space, is_discrete, action_space, config):
        assert not is_discrete  

        self.name = name
        self.actions_low = action_space.low
        self.actions_high = action_space.high
        self.env_name = config['ENV_NAME']
        self.ppo = config['PPO']
        self.is_adaptive_lr = config['LR_SCHEDULE'] == 'ADAPTIVE'
        self.is_polynom_decay_lr = config['LR_SCHEDULE'] == 'POLYNOM_DECAY'
        self.is_exp_decay_lr = config['LR_SCHEDULE'] == 'EXP_DECAY'
        self.lr_multiplier = tf.constant(1, shape=(), dtype=tf.float32)

        self.e_clip = config['E_CLIP']
        self.clip_value = config['CLIP_VALUE']
        self.network = config['NETWORK']
        self.rewards_shaper = config['REWARD_SHAPER']
        self.num_actors = config['NUM_ACTORS']
        self.vec_env = vecenv.create_vec_env(self.env_name, self.num_actors)
        self.steps_num = config['STEPS_NUM']
        self.normalize_advantage = config['NORMALIZE_ADVANTAGE']
        self.config = config
        self.state_shape = observation_space.shape
        self.critic_coef = config['CRITIC_COEF']
        self.writer = SummaryWriter()
        self.sess = sess
        self.grad_norm = config['GRAD_NORM']
        self.gamma = self.config['GAMMA']
        self.tau = self.config['TAU']
        self.normalize_input = self.config['NORMALIZE_INPUT']
        self.seq_len = self.config['SEQ_LEN']
        self.dones = np.asarray([False]*self.num_actors, dtype=np.bool)

        self.current_rewards = np.asarray([0]*self.num_actors, dtype=np.float32)  
        self.game_rewards = deque([], maxlen=100)
        self.obs_ph = tf.placeholder('float32', (None, ) + self.state_shape, name = 'obs')
        self.target_obs_ph = tf.placeholder('float32', (None, ) + self.state_shape, name = 'target_obs')
        self.actions_num = action_space.shape[0]   
        self.actions_ph = tf.placeholder('float32', (None,) + action_space.shape, name = 'actions')
        self.old_mu_ph = tf.placeholder('float32', (None,) + action_space.shape, name = 'old_mu_ph')
        self.old_sigma_ph = tf.placeholder('float32', (None,) + action_space.shape, name = 'old_sigma_ph')
        self.old_logp_actions_ph = tf.placeholder('float32', (None, ), name = 'old_logpactions')
        self.rewards_ph = tf.placeholder('float32', (None,), name = 'rewards')
        self.old_values_ph = tf.placeholder('float32', (None,), name = 'old_values')
        self.advantages_ph = tf.placeholder('float32', (None,), name = 'advantages')
        self.learning_rate_ph = tf.placeholder('float32', (), name = 'lr_ph')
        self.epoch_num = tf.Variable( tf.constant(0, shape=(), dtype=tf.int32), trainable=False)
        self.update_epoch_op = self.epoch_num.assign(self.epoch_num + 1)
        self.current_lr = self.learning_rate_ph

        if self.is_adaptive_lr:
            self.lr_threshold = config['LR_THRESHOLD']
        if self.is_polynom_decay_lr:
            self.lr_multiplier = tf.train.polynomial_decay(self.lr_multiplier, config['MAX_EPOCHS'], self.epoch_num, end_learning_rate=0.0001, power=tr_helpers.get_or_default(config, 'DECAY_PWOER', 1.0))
        if self.is_exp_decay_lr:
            self.lr_multiplier = tf.train.exponential_decay(self.lr_multiplier, config['MAX_EPOCHS'], self.epoch_num, decay_rate = config['DECAY_RATE'])

        if self.normalize_input:
            self.moving_mean_std = MovingMeanStd(shape = observation_space.shape, epsilon = 1e-5, decay = 0.99)
            self.input_obs = self.moving_mean_std.normalize(self.obs_ph, train=True)
            self.input_target_obs = self.moving_mean_std.normalize(self.target_obs_ph, train=False)
        else:
            self.input_obs = self.obs_ph
            self.input_target_obs = self.target_obs_ph

        games_num = self.config['MINIBATCH_SIZE'] // self.seq_len # it is used only for current rnn implementation

        self.train_dict = {
            'name' : 'agent',
            'inputs' : self.input_obs,
            'batch_num' : self.config['MINIBATCH_SIZE'],
            'games_num' : games_num,
            'actions_num' : self.actions_num,
            'prev_actions_ph' : self.actions_ph
        }

        self.run_dict = {
            'name' : 'agent',
            'inputs' : self.input_target_obs,
            'batch_num' : self.num_actors,
            'games_num' : self.num_actors,
            'actions_num' : self.actions_num,
            'prev_actions_ph' : None
        }

        self.states = None
        if self.network.is_rnn():
            self.logp_actions ,self.state_values, self.action, self.entropy, self.mu, self.sigma, self.states_ph, self.masks_ph, self.lstm_state, self.initial_state = self.network(self.train_dict, reuse=False)
            self.target_neglogp, self.target_state_values, self.target_action, _, self.target_mu, self.target_sigma, self.target_states_ph, self.target_masks_ph, self.target_lstm_state, self.target_initial_state = self.network(self.run_dict, reuse=True)
            self.states = self.target_initial_state
        else:
            self.logp_actions ,self.state_values, self.action, self.entropy, self.mu, self.sigma  = self.network(self.train_dict, reuse=False)
            self.target_neglogp, self.target_state_values, self.target_action, _, self.target_mu, self.target_sigma  = self.network(self.run_dict, reuse=True)

        curr_e_clip = self.e_clip * self.lr_multiplier
        if (self.ppo):
            self.prob_ratio = tf.exp(self.old_logp_actions_ph - self.logp_actions)
            self.prob_ratio = tf.clip_by_value(self.prob_ratio, 0.0, 16.0)
            self.pg_loss_unclipped = -tf.multiply(self.advantages_ph, self.prob_ratio)
            self.pg_loss_clipped = -tf.multiply(self.advantages_ph, tf.clip_by_value(self.prob_ratio, 1.- curr_e_clip, 1.+ curr_e_clip))
            self.actor_loss = tf.reduce_mean(tf.maximum(self.pg_loss_unclipped, self.pg_loss_clipped))
        else:
            self.actor_loss = tf.reduce_mean(self.logp_actions * self.advantages_ph)


        self.c_loss = (tf.squeeze(self.state_values) - self.rewards_ph)**2
        if self.clip_value:
            self.cliped_values = self.old_values_ph + tf.clip_by_value(tf.squeeze(self.state_values) - self.old_values_ph, - curr_e_clip, curr_e_clip)
            self.c_loss_clipped = tf.square(self.cliped_values - self.rewards_ph)
            self.critic_loss = tf.reduce_mean(tf.maximum(self.c_loss, self.c_loss_clipped))
        else:
            self.critic_loss = tf.reduce_mean(self.c_loss)
        
        self.kl_dist = policy_kl_tf(self.mu, self.sigma, self.old_mu_ph, self.old_sigma_ph)
        if self.is_adaptive_lr:
            self.current_lr = tf.where(self.kl_dist > (2.0 * self.lr_threshold), tf.maximum(self.current_lr / 1.5, 1e-6), self.current_lr)
            self.current_lr = tf.where(self.kl_dist < (0.5 * self.lr_threshold), tf.minimum(self.current_lr * 1.5, 1e-2), self.current_lr)
        self.loss = self.actor_loss + 0.5 * self.critic_coef * self.critic_loss - self.config['ENTROPY_COEF'] * self.entropy
        self.train_step = tf.train.AdamOptimizer(self.current_lr * self.lr_multiplier)
        self.weights = tf.get_collection(tf.GraphKeys.TRAINABLE_VARIABLES, scope='agent')

        grads = tf.gradients(self.loss, self.weights)
        if self.config['TRUNCATE_GRADS']:
            grads, _ = tf.clip_by_global_norm(grads, self.grad_norm)
        grads = list(zip(grads, self.weights))
        self.train_op = self.train_step.apply_gradients(grads)
        self.saver = tf.train.Saver()
        self.sess.run(tf.global_variables_initializer())

    def update_epoch(self):
        return self.sess.run([self.update_epoch_op])[0]

    def get_action_values(self, obs):
        run_ops = [self.target_action, self.target_state_values, self.target_neglogp, self.target_mu, self.target_sigma]
        if self.network.is_rnn():
            run_ops.append(self.target_lstm_state)
            return self.sess.run(run_ops, {self.target_obs_ph : obs, self.target_states_ph : self.states, self.target_masks_ph : self.dones})
        else:
            return (*self.sess.run(run_ops, {self.target_obs_ph : obs}), None)

    def get_values(self, obs):
        if self.network.is_rnn():
            return self.sess.run([self.target_state_values], {self.target_obs_ph : obs, self.target_states_ph : self.states, self.target_masks_ph : self.dones})
        else:
            return self.sess.run([self.target_state_values], {self.target_obs_ph : obs})

    def play_steps(self):
        # Here, we init the lists that will contain the mb of experiences
        mb_obs, mb_rewards, mb_actions, mb_values, mb_dones, mb_neglogpacs, mb_mus, mb_sigmas = [],[],[],[],[],[],[],[]
        mb_states = []
        epinfos = []
        # For n in range number of steps
        for _ in range(self.steps_num):
            if self.network.is_rnn():
                mb_states.append(self.states)

            actions, values, neglogpacs, mu, sigma, self.states = self.get_action_values(self.obs)
            actions = np.squeeze(actions)
            values = np.squeeze(values)
            neglogpacs = np.squeeze(neglogpacs)
            mb_obs.append(self.obs.copy())
            mb_actions.append(actions)
            mb_values.append(values)
            mb_neglogpacs.append(neglogpacs)
            mb_dones.append(self.dones)
            mb_mus.append(mu)
            mb_sigmas.append(sigma)


            self.obs[:], rewards, self.dones, infos = self.vec_env.step(rescale_actions(self.actions_low, self.actions_high, actions))
            self.current_rewards += rewards

            for reward, done in zip(self.current_rewards, self.dones):
                if done:
                    self.game_rewards.append(reward)

            self.current_rewards = self.current_rewards * (1.0 -self.dones)

            shaped_rewards = self.rewards_shaper(rewards)
            epinfos.append(infos)
            mb_rewards.append(shaped_rewards)

        #using openai baseline approach
        mb_obs = np.asarray(mb_obs, dtype=self.obs.dtype)
        mb_rewards = np.asarray(mb_rewards, dtype=np.float32)
        mb_actions = np.asarray(mb_actions, dtype=np.float32)
        mb_values = np.asarray(mb_values, dtype=np.float32)
        mb_neglogpacs = np.asarray(mb_neglogpacs, dtype=np.float32)
        mb_mus = np.asarray(mb_mus, dtype=np.float32)
        mb_sigmas = np.asarray(mb_sigmas, dtype=np.float32)
        mb_dones = np.asarray(mb_dones, dtype=np.bool)
        mb_states = np.asarray(mb_states, dtype=np.float32)
        last_values = self.get_values(self.obs)
        last_values = np.squeeze(last_values)
   

        mb_returns = np.zeros_like(mb_rewards)
        mb_advs = np.zeros_like(mb_rewards)
        lastgaelam = 0
        
        for t in reversed(range(self.steps_num)):
            if t == self.steps_num - 1:
                nextnonterminal = 1.0 - self.dones
                nextvalues = last_values
            else:
                nextnonterminal = 1.0 - mb_dones[t+1]
                nextvalues = mb_values[t+1]
            
            delta = mb_rewards[t] + self.gamma * nextvalues * nextnonterminal  - mb_values[t]
            mb_advs[t] = lastgaelam = delta + self.gamma * self.tau * nextnonterminal * lastgaelam

        mb_returns = mb_advs + mb_values
        if self.network.is_rnn():
            result = (*map(swap_and_flatten01, (mb_obs, mb_returns, mb_dones, mb_actions, mb_values, mb_neglogpacs, mb_mus, mb_sigmas, mb_states )), epinfos)
        else:
            result = (*map(swap_and_flatten01, (mb_obs, mb_returns, mb_dones, mb_actions, mb_values, mb_neglogpacs, mb_mus, mb_sigmas)), None, epinfos)

        return result

    def get_action(self, state, det = False):
        action = self.sess.run(self.action, {self.obs_ph: state})
        return rescale_actions(self.actions_low, self.actions_high, action)


    def save(self, fn):
        self.saver.save(self.sess, fn)

    def restore(self, fn):
        self.saver.restore(self.sess, fn)

    def train(self):
        self.obs = self.vec_env.reset()
        batch_size = self.steps_num * self.num_actors
        minibatch_size = self.config['MINIBATCH_SIZE']
        mini_epochs_num = self.config['MINI_EPOCHS']
        num_minibatches = batch_size // minibatch_size
        last_lr = self.config['LEARNING_RATE']
        frame = 0
        update_time = 0
        last_mean_rewards = -100500
        play_time = 0
        epoch_num = 0
        while True:
            play_time_start = time.time()
            epoch_num = self.update_epoch()
            frame += batch_size
            obses, returns, dones, actions, values, neglogpacs, mus, sigmas, lstm_states, _ = self.play_steps()
            advantages = returns - values
            if self.normalize_advantage:
                advantages = (advantages - advantages.mean()) / (advantages.std() + 1e-8)

            a_losses = []
            c_losses = []
            entropies = []
            kls = []
            play_time_end = time.time()
            play_time = play_time_end - play_time_start
            update_time_start = time.time()
            if self.network.is_rnn():
                total_games = batch_size // self.seq_len
                num_games_batch = minibatch_size // self.seq_len
                game_indexes = np.arange(total_games)
                flat_indexes = np.arange(total_games * self.seq_len).reshape(total_games, self.seq_len)
                lstm_states = lstm_states[::self.seq_len]
                for _ in range(0, mini_epochs_num):
                    np.random.shuffle(game_indexes)

                    for i in range(0, num_minibatches):
                        batch = range(i * num_games_batch, (i + 1) * num_games_batch)
                        mb_indexes = game_indexes[batch]
                        mbatch = flat_indexes[mb_indexes].ravel()                        

                        dict = {}
                        dict[self.old_values_ph] = values[mbatch]
                        dict[self.old_logp_actions_ph] = neglogpacs[mbatch]
                        dict[self.advantages_ph] = advantages[mbatch]
                        dict[self.rewards_ph] = returns[mbatch]
                        dict[self.actions_ph] = actions[mbatch]
                        dict[self.obs_ph] = obses[mbatch]
                        dict[self.old_mu_ph] = mus[mbatch]
                        dict[self.old_sigma_ph] = sigmas[mbatch]
                        dict[self.masks_ph] = dones[mbatch]
                        dict[self.states_ph] = lstm_states[batch]
                        
                        dict[self.learning_rate_ph] = last_lr
                        run_ops = [self.actor_loss, self.critic_loss, self.entropy, self.kl_dist, self.current_lr, self.mu, self.sigma, self.lr_multiplier, self.train_op]
                        run_ops.append(tf.get_collection(tf.GraphKeys.UPDATE_OPS))
                        a_loss, c_loss, entropy, kl, last_lr, cmu, csigma, lr_mul, _, _ = self.sess.run(run_ops, dict)
                        mus[mbatch] = cmu
                        sigmas[mbatch] = csigma
                        a_losses.append(a_loss)
                        c_losses.append(c_loss)
                        kls.append(kl)
                        entropies.append(entropy)
            else:
                for _ in range(0, mini_epochs_num):
                    permutation = np.random.permutation(batch_size)
                    obses = obses[permutation]
                    returns = returns[permutation]
                    
                    actions = actions[permutation]
                    values = values[permutation]
                    neglogpacs = neglogpacs[permutation]
                    advantages = advantages[permutation]
                    mus = mus[permutation]
                    sigmas = sigmas[permutation] 
                    if self.network.is_rnn():
                        lstm_states = lstm_states[permutation] 
                        dones = dones[permutation]
                        
                    for i in range(0, num_minibatches):
                        batch = range(i * minibatch_size, (i + 1) * minibatch_size)
                        dict = {self.obs_ph: obses[batch], self.actions_ph : actions[batch], self.rewards_ph : returns[batch], 
                                self.advantages_ph : advantages[batch], self.old_logp_actions_ph : neglogpacs[batch], self.old_values_ph : values[batch]}

                        dict[self.old_mu_ph] = mus[batch]
                        dict[self.old_sigma_ph] = sigmas[batch]
                        dict[self.learning_rate_ph] = last_lr
                        run_ops = [self.actor_loss, self.critic_loss, self.entropy, self.kl_dist, self.current_lr, self.mu, self.sigma, self.lr_multiplier, self.train_op]
                        

                        if self.network.is_rnn():
                            dict[self.states_ph] = lstm_states[batch]
                            dict[self.masks_ph] = dones[batch]
                            
                        run_ops.append(tf.get_collection(tf.GraphKeys.UPDATE_OPS))
                        a_loss, c_loss, entropy, kl, last_lr, cmu, csigma, lr_mul, _, _ = self.sess.run(run_ops, dict)
                        mus[batch] = cmu
                        sigmas[batch] = csigma
                        a_losses.append(a_loss)
                        c_losses.append(c_loss)
                        kls.append(kl)
                        entropies.append(entropy)
            update_time_end = time.time()
            update_time = update_time_end - update_time_start
            sum_time = update_time + play_time

            if True:
                print('Frames per seconds: ', batch_size / sum_time)
                self.writer.add_scalar('performance/fps', batch_size / sum_time, frame)
                self.writer.add_scalar('performance/upd_time', update_time, frame)
                self.writer.add_scalar('performance/play_time', play_time, frame)
                self.writer.add_scalar('losses/a_loss', np.mean(a_losses), frame)
                self.writer.add_scalar('losses/c_loss', np.mean(c_losses), frame)
                self.writer.add_scalar('losses/entropy', np.mean(entropies), frame)
                self.writer.add_scalar('info/last_lr', last_lr * lr_mul, frame)
                self.writer.add_scalar('info/e_clip', self.e_clip * lr_mul, frame)
                self.writer.add_scalar('info/kl', np.mean(kls), frame)
                self.writer.add_scalar('epochs', epoch_num, frame)
                if len(self.game_rewards) > 0:
                    mean_rewards = np.mean(self.game_rewards)
                    self.writer.add_scalar('rewards/mean_100', mean_rewards, frame)
                    if mean_rewards > last_mean_rewards:
                        print('saving next best rewards: ', mean_rewards)
                        last_mean_rewards = mean_rewards
                        self.save("./nn/" + self.name + self.env_name)
                        if last_mean_rewards > self.config['SCORE_TO_WIN']:
                            print('Network won!')
                            return

                update_time = 0

            
        
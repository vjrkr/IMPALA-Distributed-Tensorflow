import tensorflow as tf
import numpy as np

import tensorboardX
import buffer_queue
import collections
import py_process
import wrappers
import config
import model
import time
import gym

flags = tf.app.flags
FLAGS = tf.app.flags.FLAGS



flags.DEFINE_integer('num_actors', 4, 'Number of actors.')
flags.DEFINE_integer('task', -1, 'Task id. Use -1 for local training.')
flags.DEFINE_integer('batch_size', 32, 'how many batch learner should be training')
flags.DEFINE_integer('queue_size', 128, 'fifoqueue size')
flags.DEFINE_integer('trajectory', 20, 'trajectory length')
flags.DEFINE_integer('learning_frame', int(1e9), 'trajectory length')
flags.DEFINE_integer('lstm_size', 256, 'lstm_size')

flags.DEFINE_float('start_learning_rate', 0.0006, 'start_learning_rate')
flags.DEFINE_float('end_learning_rate', 0, 'end_learning_rate')
flags.DEFINE_float('discount_factor', 0.99, 'discount factor')
flags.DEFINE_float('entropy_coef', 0.05, 'entropy coefficient')
flags.DEFINE_float('baseline_loss_coef', 0.5, 'baseline coefficient')
flags.DEFINE_float('gradient_clip_norm', 40.0, 'gradient clip norm')

flags.DEFINE_enum('job_name', 'learner', ['learner', 'actor'], 'Job name. Ignored when task is set to -1')
flags.DEFINE_enum('reward_clipping', 'abs_one', ['abs_one', 'soft_asymmetric'], 'Reward clipping.')

def main(_):

    local_job_device = '/job:{}/task:{}'.format(FLAGS.job_name, FLAGS.task)
    shared_job_device = '/job:learner/task:0'
    is_actor_fn = lambda i: FLAGS.job_name == 'actor' and i == FLAGS.task
    is_learner = FLAGS.job_name == 'learner'

    cluster = tf.train.ClusterSpec({
        'actor': ['localhost:{}'.format(8001+i) for i in range(FLAGS.num_actors)],
        'learner': ['localhost:8000']})

    server = tf.train.Server(cluster, job_name=FLAGS.job_name, task_index=FLAGS.task)

    filters = [shared_job_device, local_job_device]

    output_size = 18
    available_output_size = 6
    env_name = 'DemonAttackDeterministic-v4'
    input_shape = [84, 84, 4]

    with tf.device(shared_job_device):
        
        with tf.device('/cpu'):
            queue = buffer_queue.FIFOQueue(
                FLAGS.trajectory, input_shape, output_size,
                FLAGS.queue_size, FLAGS.batch_size,
                FLAGS.num_actors, FLAGS.lstm_size)
                
        learner = model.IMPALA(
            trajectory=FLAGS.trajectory,
            input_shape=input_shape,
            num_action=output_size,
            discount_factor=FLAGS.discount_factor,
            start_learning_rate=FLAGS.start_learning_rate,
            end_learning_rate=FLAGS.end_learning_rate,
            learning_frame=FLAGS.learning_frame,
            baseline_loss_coef=FLAGS.baseline_loss_coef,
            entropy_coef=FLAGS.entropy_coef,
            gradient_clip_norm=FLAGS.gradient_clip_norm,
            reward_clipping=FLAGS.reward_clipping,
            model_name='learner',
            learner_name='learner',
            lstm_hidden_size=FLAGS.lstm_size)
    
    with tf.device(local_job_device):
        actor = model.IMPALA(
                trajectory=FLAGS.trajectory,
                input_shape=input_shape,
                num_action=output_size,
                discount_factor=FLAGS.discount_factor,
                start_learning_rate=FLAGS.start_learning_rate,
                end_learning_rate=FLAGS.end_learning_rate,
                learning_frame=FLAGS.learning_frame,
                baseline_loss_coef=FLAGS.baseline_loss_coef,
                entropy_coef=FLAGS.entropy_coef,
                gradient_clip_norm=FLAGS.gradient_clip_norm,
                reward_clipping=FLAGS.reward_clipping,
                model_name='actor_{}'.format(FLAGS.task),
                learner_name='learner',
                lstm_hidden_size=FLAGS.lstm_size)

    sess = tf.Session(server.target)
    queue.set_session(sess)
    learner.set_session(sess)

    if not is_learner:
        actor.set_session(sess)

    if is_learner:

        writer = tensorboardX.SummaryWriter('runs/learner')
        train_step = 0

        while True:
            size = queue.get_size()
            if size > 3 * FLAGS.batch_size:
                train_step += 1
                batch = queue.sample_batch()
                s = time.time()
                pi_loss, baseline_loss, entropy, learning_rate = learner.train(
                                                                    state=np.stack(batch.state),
                                                                    reward=np.stack(batch.reward),
                                                                    action=np.stack(batch.action),
                                                                    done=np.stack(batch.done),
                                                                    behavior_policy=np.stack(batch.behavior_policy),
                                                                    previous_action=np.stack(batch.previous_action),
                                                                    initial_h=np.stack(batch.previous_h),
                                                                    initial_c=np.stack(batch.previous_c))
                writer.add_scalar('data/pi_loss', pi_loss, train_step)
                writer.add_scalar('data/baseline_loss', baseline_loss, train_step)
                writer.add_scalar('data/entropy', entropy, train_step)
                writer.add_scalar('data/learning_rate', learning_rate, train_step)
                writer.add_scalar('data/time', time.time() - s, train_step)
    else:

        trajectory_data = collections.namedtuple(
                'trajectory_data',
                ['state', 'next_state', 'reward', 'done',
                 'action', 'behavior_policy', 'previous_action',
                 'initial_h', 'initial_c'])

        env = wrappers.make_uint8_env(env_name)
        if FLAGS.task == 0:
            env = gym.wrappers.Monitor(env, 'save-mov', video_callable=lambda episode_id: episode_id%10==0)
        state = env.reset()
        previous_action = 0
        previous_h = np.zeros([FLAGS.lstm_size])
        previous_c = np.zeros([FLAGS.lstm_size])

        episode = 0
        score = 0
        episode_step = 0
        total_max_prob = 0
        lives = 4
        
        writer = tensorboardX.SummaryWriter('runs/{}/actor_{}'.format(env_name, FLAGS.task))

        while True:

            unroll_data = trajectory_data(
                [], [], [], [],
                [], [], [] ,[], [])

            actor.parameter_sync()

            for _ in range(FLAGS.trajectory):

                action, behavior_policy, max_prob, h, c = actor.get_policy_and_action(
                    state, previous_action, previous_h, previous_c)

                episode_step += 1
                total_max_prob += max_prob

                next_state, reward, done, info = env.step(action % available_output_size)

                score += reward

                if lives != info['ale.lives']:
                    r = -1
                    d = True
                else:
                    r = reward
                    d = False

                unroll_data.state.append(state)
                unroll_data.next_state.append(next_state)
                unroll_data.reward.append(r)
                unroll_data.done.append(d)
                unroll_data.action.append(action)
                unroll_data.behavior_policy.append(behavior_policy)
                unroll_data.previous_action.append(previous_action)
                unroll_data.initial_h.append(previous_h)
                unroll_data.initial_c.append(previous_c)

                state = next_state
                previous_action = action
                previous_h = h
                previous_c = c
                lives = info['ale.lives']

                if done:
                    
                    print(episode, score)
                    writer.add_scalar('data/{}/prob'.format(env_name), total_max_prob / episode_step, episode)
                    writer.add_scalar('data/{}/score'.format(env_name), score, episode)
                    writer.add_scalar('data/{}/episode_step'.format(env_name), episode_step, episode)
                    episode += 1
                    score = 0
                    episode_step = 0
                    total_max_prob = 0
                    lives = 4
                    state = env.reset()
                    previous_action = 0
                    previous_h = np.zeros([FLAGS.lstm_size])
                    previous_c = np.zeros([FLAGS.lstm_size])

            queue.append_to_queue(
                task=FLAGS.task, unrolled_state=unroll_data.state,
                unrolled_next_state=unroll_data.next_state, unrolled_reward=unroll_data.reward,
                unrolled_done=unroll_data.done, unrolled_action=unroll_data.action,
                unrolled_behavior_policy=unroll_data.behavior_policy,
                unrolled_previous_action=unroll_data.previous_action,
                unrolled_previous_h=unroll_data.initial_h,
                unrolled_previous_c=unroll_data.initial_c)

if __name__ == '__main__':
    tf.app.run()

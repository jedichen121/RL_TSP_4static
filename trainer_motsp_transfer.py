"""Defines the main trainer model for combinatorial problems

Each task must define the following functions:
* mask_fn: can be None
* update_fn: can be None
* reward_fn: specifies the quality of found solutions
* render_fn: Specifies how to plot found solutions. Can be None
"""

import os
import time
import argparse
import datetime
import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.optim as optim
from torch.utils.data import DataLoader

from model import DRL4TSP, Encoder

device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
# device = torch.device('cpu')


class StateCritic(nn.Module):
    """Estimates the problem complexity.

    This is a basic module that just looks at the log-probabilities predicted by
    the encoder + decoder, and returns an estimate of complexity
    """

    def __init__(self, static_size, dynamic_size, hidden_size):
        super(StateCritic, self).__init__()

        self.static_encoder = Encoder(static_size, hidden_size)
        self.dynamic_encoder = Encoder(dynamic_size, hidden_size)

        # Define the encoder & decoder models
        self.fc1 = nn.Conv1d(hidden_size * 2, 20, kernel_size=1)
        self.fc2 = nn.Conv1d(20, 20, kernel_size=1)
        self.fc3 = nn.Conv1d(20, 1, kernel_size=1)

        for p in self.parameters():
            if len(p.shape) > 1:
                nn.init.xavier_uniform_(p)

    def forward(self, static, dynamic):

        # Use the probabilities of visiting each
        static_hidden = self.static_encoder(static)
        dynamic_hidden = self.dynamic_encoder(dynamic)

        hidden = torch.cat((static_hidden, dynamic_hidden), 1)

        output = F.relu(self.fc1(hidden))
        output = F.relu(self.fc2(output))
        output = self.fc3(output).sum(dim=2)
        return output


def validate(data_loader, actor, reward_fn, w1, w2, render_fn=None, save_dir='.',
             num_plot=5):
    """Used to monitor progress on a validation set & optionally plot solution."""

    actor.eval()

    # if not os.path.exists(save_dir):
    #     os.makedirs(save_dir)

    rewards = []
    obj1s = []
    obj2s = []
    for batch_idx, batch in enumerate(data_loader):

        static, dynamic, x0 = batch

        static = static.to(device)
        dynamic = dynamic.to(device)
        x0 = x0.to(device) if len(x0) > 0 else None

        with torch.no_grad():
            tour_indices, _ = actor.forward(static, dynamic, x0)

        reward, obj1, obj2 = reward_fn(static, tour_indices, w1, w2)

        rewards.append(torch.mean(reward.detach()).item())
        obj1s.append(torch.mean(obj1.detach()).item())
        obj2s.append(torch.mean(obj2.detach()).item())
        # if render_fn is not None and batch_idx < num_plot:
        #     name = 'batch%d_%2.4f.png'%(batch_idx, torch.mean(reward.detach()).item())
        #     path = os.path.join(save_dir, name)
        #     render_fn(static, tour_indices, path)

    actor.train()
    return np.mean(rewards), np.mean(obj1s), np.mean(obj2s)


def train(actor, critic, w1, w2, task, num_nodes, train_data, valid_data, reward_fn,
          render_fn, batch_size, actor_lr, critic_lr, max_grad_norm,
          **kwargs):
    """Constructs the main actor & critic networks, and performs all training."""

    now = '%s' % datetime.datetime.now().time()
    now = now.replace(':', '_')
    bname = "_transfer"
    save_dir = os.path.join(task+bname, '%d' % num_nodes, 'w_%2.2f_%2.2f' % (w1, w2), now)
    print("save_dir is ", save_dir)
    checkpoint_dir = os.path.join(save_dir, 'checkpoints')
    if not os.path.exists(checkpoint_dir):
         os.makedirs(checkpoint_dir)

    actor_optim = optim.Adam(actor.parameters(), lr=actor_lr)
    critic_optim = optim.Adam(critic.parameters(), lr=critic_lr)

    train_loader = DataLoader(train_data, batch_size, True, num_workers=0)
    valid_loader = DataLoader(valid_data, batch_size, False, num_workers=0)

    best_params = None
    best_reward = np.inf
    start_total = time.time()
    for epoch in range(3):
        print("epoch %d start:"% epoch)
        # Jiyang: not sure what this actually do
        actor.train()
        critic.train()

        times, losses, rewards, critic_rewards = [], [], [], []
        obj1s, obj2s = [], []

        epoch_start = time.time()
        start = epoch_start

        for batch_idx, batch in enumerate(train_loader):

            static, dynamic, x0 = batch

            static = static.to(device)
            dynamic = dynamic.to(device)
            x0 = x0.to(device) if len(x0) > 0 else None

            # Full forward pass through the dataset
            tour_indices, tour_logp = actor(static, dynamic, x0)

            # Sum the log probabilities for each city in the tour
            reward, obj1, obj2 = reward_fn(static, tour_indices, w1, w2)

            # Query the critic for an estimate of the reward
            critic_est = critic(static, dynamic).view(-1)

            advantage = (reward - critic_est)
            actor_loss = torch.mean(advantage.detach() * tour_logp.sum(dim=1))
            critic_loss = torch.mean(advantage ** 2)

            actor_optim.zero_grad()
            actor_loss.backward()
            torch.nn.utils.clip_grad_norm_(actor.parameters(), max_grad_norm)
            actor_optim.step()

            critic_optim.zero_grad()
            critic_loss.backward()
            torch.nn.utils.clip_grad_norm_(critic.parameters(), max_grad_norm)
            critic_optim.step()

            critic_rewards.append(torch.mean(critic_est.detach()).item())
            rewards.append(torch.mean(reward.detach()).item())
            losses.append(torch.mean(actor_loss.detach()).item())
            obj1s.append(torch.mean(obj1.detach()).item())
            obj2s.append(torch.mean(obj2.detach()).item())
            if (batch_idx + 1) % 200 == 0:
                print("\n")
                end = time.time()
                times.append(end - start)
                start = end

                mean_loss = np.mean(losses[-100:])
                mean_reward = np.mean(rewards[-100:])
                mean_obj1 = np.mean(obj1s[-100:])
                mean_obj2 = np.mean(obj2s[-100:])
                print('  Batch %d/%d, reward: %2.3f, obj1: %2.3f, obj2: %2.3f, loss: %2.4f, took: %2.4fs' %
                      (batch_idx, len(train_loader), mean_reward, mean_obj1, mean_obj2, mean_loss,
                       times[-1]))

        mean_loss = np.mean(losses)
        mean_reward = np.mean(rewards)

        # Save the weights
        # epoch_dir = os.path.join(checkpoint_dir, '%s' % epoch)
        # if not os.path.exists(epoch_dir):
        #     os.makedirs(epoch_dir)
        #
        # save_path = os.path.join(epoch_dir, 'actor.pt')
        # torch.save(actor.state_dict(), save_path)
        #
        # save_path = os.path.join(epoch_dir, 'critic.pt')
        # torch.save(critic.state_dict(), save_path)

        # Save rendering of validation set tours
        # valid_dir = os.path.join(save_dir, '%s' % epoch)
        mean_valid, mean_obj1_valid, mean_obj2_valid = validate(valid_loader, actor, reward_fn, w1, w2, render_fn,
                              '.', num_plot=5)

        # Save best model parameters
        if mean_valid < best_reward:

            best_reward = mean_valid

            # save_path = os.path.join(save_dir, 'actor.pt')
            # torch.save(actor.state_dict(), save_path)
            #
            # save_path = os.path.join(save_dir, 'critic.pt')
            # torch.save(critic.state_dict(), save_path)
            # 存在w_1_0主文件夹下，多存一份，用来transfer to next w
            main_dir = os.path.join(task+bname, '%d' % num_nodes, 'w_%2.2f_%2.2f' % (w1, w2))
            save_path = os.path.join(main_dir, 'actor.pt')
            torch.save(actor.state_dict(), save_path)
            save_path = os.path.join(main_dir, 'critic.pt')
            torch.save(critic.state_dict(), save_path)

        print('Mean epoch loss/reward: %2.4f, %2.4f, %2.4f, obj1_valid: %2.3f, obj2_valid: %2.3f. took: %2.4fs '\
              '(%2.4fs / 100 batches)\n' % \
              (mean_loss, mean_reward, mean_valid, mean_obj1_valid, mean_obj2_valid, time.time() - epoch_start,
              np.mean(times)))
    print("Total run time of epoches: %2.4f" % (time.time() - start_total))



def train_tsp(args, w1=1, w2=0, checkpoint = None):

    # Goals from paper:
    # TSP20, 3.97
    # TSP50, 6.08
    # TSP100, 8.44

    from tasks import motsp
    from tasks.motsp import TSPDataset

    # Jiyang: static_size is input dimension (4 in the paper)
    STATIC_SIZE = 4 # (x, y)
    DYNAMIC_SIZE = 1 # dummy for compatibility

    # Jiyang: generate random datas
    train_data = TSPDataset(args.num_nodes, args.train_size, args.seed)
    valid_data = TSPDataset(args.num_nodes, args.valid_size, args.seed + 1)

    update_fn = None

    actor = DRL4TSP(STATIC_SIZE,
                    DYNAMIC_SIZE,
                    args.hidden_size,
                    update_fn,
                    motsp.update_mask,
                    args.num_layers,
                    args.dropout).to(device)

    # Jiyang: don't understand what this does
    critic = StateCritic(STATIC_SIZE, DYNAMIC_SIZE, args.hidden_size).to(device)

    kwargs = vars(args)
    kwargs['train_data'] = train_data
    kwargs['valid_data'] = valid_data
    kwargs['reward_fn'] = motsp.reward
    kwargs['render_fn'] = motsp.render

    if checkpoint:
        path = os.path.join(checkpoint, 'actor.pt')
        actor.load_state_dict(torch.load(path, device))
        # actor.static_encoder.state_dict().get("conv.weight").size()
        path = os.path.join(checkpoint, 'critic.pt')
        critic.load_state_dict(torch.load(path, device))

    if not args.test:
        # Jiyang: do the actual training
        train(actor, critic, w1, w2, **kwargs)

    test_data = TSPDataset(args.num_nodes, args.valid_size, args.seed + 2)

    test_dir = 'test'
    # Jiyang: DataLoader is from Pytorch, a data loading utility
    test_loader = DataLoader(test_data, args.valid_size, False, num_workers=0)
    out = validate(test_loader, actor, motsp.reward, w1, w2, motsp.render, test_dir, num_plot=5)

    print('w1=%2.2f,w2=%2.2f. Average tour length: ' % (w1, w2), out)


if __name__ == '__main__':
    # Jiyang: specifies how many nodes in TSP
    num_nodes = 20
    # num_nodes = 100
    parser = argparse.ArgumentParser(description='Combinatorial Optimization')
    parser.add_argument('--seed', default=12345, type=int)
    # parser.add_argument('--checkpoint', default="tsp/20/w_1_0/20_06_30.888074")
    parser.add_argument('--test', action='store_true', default=False)
    parser.add_argument('--task', default='tsp')
    parser.add_argument('--nodes', dest='num_nodes', default=num_nodes, type=int)
    parser.add_argument('--actor_lr', default=5e-4, type=float)
    parser.add_argument('--critic_lr', default=5e-4, type=float)
    parser.add_argument('--max_grad_norm', default=2., type=float)
    parser.add_argument('--batch_size', default=200, type=int)
    parser.add_argument('--hidden', dest='hidden_size', default=128, type=int)
    parser.add_argument('--dropout', default=0.1, type=float)
    parser.add_argument('--layers', dest='num_layers', default=1, type=int)
    parser.add_argument('--train-size',default=120000, type=int)
    parser.add_argument('--valid-size', default=1000, type=int)

    args = parser.parse_args()

    # Jiyang: define the number of subproblems
    T = 100
    if args.task == 'tsp':
        w2_list = np.arange(T+1)/T
        for i in range(0,T+1):
            print("Current w:%2.2f/%2.2f"% (1-w2_list[i], w2_list[i]))
            if i==0:
                # The first subproblem can be trained from scratch. It also can be trained based on a
                # single-TSP trained model, where the model can be obtained from everywhere in github
                checkpoint = 'tsp_transfer_100run_500000_5epoch_40city/40/w_1.00_0.00'
                train_tsp(args, 1, 0, checkpoint)
            else:
                # Parameter transfer. train based on the parameters of the previous subproblem
                checkpoint = 'tsp_transfer/%d/w_%2.2f_%2.2f'%(num_nodes, 1-w2_list[i-1], w2_list[i-1])
                train_tsp(args, 1-w2_list[i], w2_list[i], checkpoint)



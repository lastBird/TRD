import torch
import torch.nn as nn
import torch.nn.functional as F
import numpy as np
import random
import pandas as pd
import math
import gc
import os
import argparse
import torch.backends.cudnn as cudnn

import sys
# sys.path.append('/home/xinghua/Hongyang/Code-submit')
sys.path.append('/home/workshop/lhy/code-submit')

from collections import defaultdict
from tqdm import tqdm

from daisy.utils.loader import load_rate, split_test, get_ur, PairMFData, get_ur_l, get_model_pred
from daisy.utils.metrics import precision_at_k, recall_at_k, map_at_k, hr_at_k, mrr_at_k, ndcg_at_k


class Net(nn.Module):
    def __init__(self, user_num, item_num, model, method, factor_num=32, gpuid='0'):
        super(Net, self).__init__()

        os.environ['CUDA_VISIBLE_DEVICES'] = gpuid
        cudnn.benchmark = True

        self.factor_num = factor_num

        self.embed_user = nn.Embedding(user_num, factor_num)
        self.embed_item = nn.Embedding(item_num + 1, factor_num, padding_idx=item_num)

        if method == 'Item2Vec' or method == 'mostpop' or method == 'itemknn' :
            item_weights = np.random.normal(size=(item_num + 1, factor_num), scale=0.01)
            for k, v in model.item2vec.items():
                if isinstance(k, int):
                    item_weights[k] = v
            self.embed_item.weight.data.copy_(torch.from_numpy(item_weights))

            user_weights = np.random.normal(size=(user_num, factor_num), scale=0.01)
            for k, v in model.user_vec_dict.items():
                if isinstance(k, int):
                    user_weights[k] = v
            self.embed_user.weight.data.copy_(torch.from_numpy(user_weights))

            del item_weights, user_weights
        elif method == 'bprmf':
            weight = model.embed_item.weight.cpu().detach()
            pad = np.random.normal(size=(1, factor_num), scale=0.01)
            pad = torch.FloatTensor(pad)
            weight = torch.cat([weight, pad])
            self.embed_item.weight.data.copy_(weight) 

            weight = model.embed_user.weight.cpu().detach()
            self.embed_user.weight.data.copy_(weight)
        elif method == 'neumf':
            weight = model.embed_item_GMF.weight.cpu().detach()
            pad = np.random.normal(size=(1, factor_num), scale=0.01)
            pad = torch.FloatTensor(pad)
            weight = torch.cat([weight, pad])
            self.embed_item.weight.data.copy_(weight) 

            weight = model.embed_user_GMF.weight.cpu().detach()
            self.embed_user.weight.data.copy_(weight)            
        else: 
            nn.init.normal_(self.embed_user.weight, std=0.01)
            nn.init.normal_(self.embed_item.weight, std=0.01)

        if self.factor_num == 32:
            self.fc1 = nn.Linear(self.factor_num * 7, 256)
            self.fc2 = nn.Linear(256,128)
            self.out = nn.Linear(128,1)
        elif self.factor_num == 8 or self.factor_num == 16:
            self.fc1 = nn.Linear(self.factor_num * 7, 128)
            self.fc2 = nn.Linear(128,64)
            self.out = nn.Linear(64,1)
        else:
            self.fc1 = nn.Linear(self.factor_num * 7, 512)
            self.fc2 = nn.Linear(512,256)
            self.out = nn.Linear(256,1)
        
        self.fc1.weight.data.normal_(0,0.1)
        self.fc2.weight.data.normal_(0,0.1)
        self.out.weight.data.normal_(0,0.1)
        
    def forward(self, x0, x1, x2, learn=False):
        x0 = self.embed_user(torch.tensor(x0).cuda().long()).view(-1, self.factor_num)
        x1 = self.embed_item(torch.tensor(x1).cuda().long()).view(-1, self.factor_num * 5)
        x2 = self.embed_item(torch.tensor(x2).cuda().long()).view(-1, self.factor_num)
        x = torch.cat([x0,x1,x2], dim=1)
        x = self.fc1(x)
        x = F.dropout(x,p=0.5)
        x = F.relu(x)
        x = self.fc2(x)
        x = F.dropout(x,p=0.5)
        x = F.relu(x)
        action_q_value = self.out(x)
        return action_q_value

        

class DQN():
    def __init__(self, user_num, item_num, model, n_actions, method,lr=0.01, epsilon=0.9, gamma=0.9, memory_capacity=20, iteration=8, batch_size=4, factor_num=32, gpuid='0',
    use_cuda=True):
        super(DQN, self).__init__()

        self.lr = lr
        self.epsilon = epsilon
        self.gamma = gamma
        self.n_actions = n_actions
        self.memory_capacity = memory_capacity
        self.iteration = iteration
        self.batch_size = batch_size
        self.factor_num = factor_num
        self.n_states = 5
        

        self.eval_net, self.target_net = Net(user_num, item_num, model, method, self.factor_num, gpuid), Net(user_num, item_num, model, method, self.factor_num, gpuid)
        
        self.use_cuda = use_cuda
        os.environ['CUDA_VISIBLE_DEVICES'] = gpuid
        cudnn.benchmark = True

        self.learn_step_counter = 0  # for target updating
        self.memory_counter = 0      # for storing memory

        self.memory = np.zeros((self.memory_capacity, self.n_states * 2 + 3 + args.n_actions))
        self.optimizer = torch.optim.Adam(self.eval_net.parameters(), lr=self.lr)

        self.action_space = []
        self.candidate_actions = []

        if torch.cuda.is_available():
            self.eval_net.cuda()
            self.target_net.cuda()
            self.loss_func = nn.MSELoss().cuda()
        else:
            self.loss_func = nn.MSELoss()
        
    def create_action_space(self, u_action_space):
        self.action_space = u_action_space[:self.n_actions]
        self.candidate_actions = u_action_space[self.n_actions:]
    
    def update_action_space(self, action):
        self.action_space.remove(action)
        add = self.candidate_actions.pop(0)
        self.action_space.append(add)
    
    def choose_action(self, user, ur, train):
        if train:
            if np.random.uniform()< self.epsilon:
                action = self.action_space
                u = torch.full([len(self.action_space), 1], user)
                s = torch.tensor(ur).view(-1, self.n_states)
                s = s.repeat(self.n_actions, 1)
                a = torch.tensor(action).view(self.n_actions, -1)
                res = self.eval_net.forward(u, s, a)
                _, indices = torch.max(res, 0)
                item = action[indices]
                return item
            else:
                item = random.choice(self.action_space)
                return item
        else:
            action = self.action_space
            u = torch.full([len(self.action_space), 1], user)
            s = torch.tensor(ur).view(-1, self.n_states)
            s = s.repeat(self.n_actions, 1)
            a = torch.tensor(action).view(self.n_actions, -1)
            res = self.eval_net.forward(u, s, a)
            _, indices = torch.max(res, 0)
            item = action[indices]
            return item
    
    # @profile
    def store_transition(self, u, s, a, r, s_, a_s):
        u = np.array(u)
        s = np.array(s)
        s_ = np.array(s_)
        a_s = np.array(a_s)
        transition = np.hstack((u, s, a, r, s_, a_s))   # horizon add
        index = self.memory_counter % self.memory_capacity
        self.memory[index, :] = transition
        self.memory_counter += 1 
    
    # @profile
    def learn(self, user):
        if self.learn_step_counter % self.iteration == 0:
            self.target_net.load_state_dict(self.eval_net.state_dict())
        self.learn_step_counter += 1

        sample_index = np.random.choice(self.memory_capacity, self.batch_size)
        b_memory = self.memory[sample_index, :]
        b_u = b_memory[:, :1].astype(int)
        b_s = torch.FloatTensor(b_memory[:, 1:self.n_states+1])
        b_a = torch.LongTensor(b_memory[:, self.n_states+1: self.n_states + 2])
        b_r = torch.FloatTensor(b_memory[:, self.n_states + 2: self.n_states + 3])
        b_s_ = torch.FloatTensor(b_memory[:, self.n_states + 3:self.n_states + 3+self.n_states])
        b_a_sp = torch.FloatTensor(b_memory[:, -self.n_actions:])   # action space with state b_s_


        q_eval = self.eval_net(b_u, b_s, b_a)
        q_next = []

        for i in range(self.batch_size):
            u = b_u[i][0]
            u = torch.full([self.n_actions, 1], u)
            s_ = b_s_[i].view(-1, self.n_states)
            s_ = s_.repeat(self.n_actions, 1)
            a_ = b_a_sp[i].view(-1, self.n_actions)
            a_ = a_.t()
            res = self.target_net(u, s_, a_).detach()
            value, _ = torch.max(res, 0)
            q_next.append(value)

        q_next = torch.tensor(q_next)
        q_target = b_r + self.gamma * q_next.view(self.batch_size, 1)

        if torch.cuda.is_available():
            q_target = q_target.cuda()
            q_eval = q_eval.cuda()

        loss = self.loss_func(q_eval, q_target)
        self.optimizer.zero_grad()
        loss.backward()
        self.optimizer.step()
        
        del b_a_sp
        gc.collect()                   

def get_next_state(state, action, t_ur):
    if action in t_ur:
        state.pop(0)
        state.append(action)
        s_next = state
    else:
        s_next = state
    return s_next 

def get_reward(rec_list, a, t_ur, u, model, pred, method):
    score = 0
    if pred:
        if method == 'neumf' or method == 'bprmf':
            score = model.predict(torch.tensor(u).cuda(), torch.tensor(a).cuda())
            score = torch.sigmoid(score)
            score = score.detach().numpy()
        elif method == 'itemknn' or method == 'Item2Vec':
            score = model.predict(u, a)
            score = 1 / (1 + np.exp(-score))
        else:
            if (u,a) in model:
                score = model[(u,a)]
                score = 1 / (1 + np.exp(-score))
    if a in t_ur:
        rel = 1
        r = np.subtract(np.power(2, rel), 1) / np.log2(len(rec_list) + 1) + score
        return r 
    else:
        r = 0
        return r

def pad_ur(ur, item_num):
    user_record = ur
    for _ in range(5 - len(ur)):
        user_record.insert(0, item_num)
    return user_record
    

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='TRD recommender test')
    # common settings
    parser.add_argument('--dataset', 
                        type=str, 
                        default='ml-100k', 
                        help='select dataset')
    parser.add_argument('--prepro', 
                        type=str, 
                        default='origin', 
                        help='dataset preprocess op.: origin/5core/10core')
    parser.add_argument('--topk', 
                        type=int, 
                        default=50, 
                        help='top number of recommend list')
    parser.add_argument('--method', 
                        type=str, 
                        default='bprmf', 
                        help='bprmf, neumf, Item2Vec, mostpop, itemknn')
    parser.add_argument('--cand_num', 
                        type=int, 
                        default=1000, 
                        help='No. of candidates item for predict')
    parser.add_argument('--factors', 
                        type=int, 
                        default=32, 
                        help='predictive factors numbers in the model')
    # algo settings
    parser.add_argument('--n_actions', 
                        type=int, 
                        default=20,
                        help='the size of action space')
    parser.add_argument('--memory_capacity', 
                        type=int, 
                        default=20, 
                        help='memory_capacity')
    parser.add_argument('--pred_score',
                        type=int, 
                        default=1,
                        help='whether add pred_score into the reward')
    parser.add_argument('--gpu', 
                        type=str, 
                        default='0', 
                        help='gpu card ID')
    args = parser.parse_args()

    '''Test Process for Metrics Exporting'''
    train_set1 = pd.read_csv(f'../experiment_data/train1_{args.dataset}_{args.prepro}.dat')
    train_set2 = pd.read_csv(f'../experiment_data/train2_{args.dataset}_{args.prepro}.dat')

    test_set = pd.read_csv(f'../experiment_data/test_{args.dataset}_{args.prepro}.dat')

    train_set1['rating'] = 1.0
    train_set2['rating'] = 1.0
    test_set['rating'] = 1.0


    split_idx_1 = len(train_set1)
    split_idx_2 = len(train_set2) + split_idx_1

    df = pd.concat([train_set1,  train_set2, test_set], ignore_index=True)
    df['user'] = pd.Categorical(df['user']).codes
    df['item'] = pd.Categorical(df['item']).codes

    user_num = df['user'].nunique()
    item_num = df['item'].nunique()

    train_set1, train_set2, test_set = df.iloc[:split_idx_1, :].copy(), df.iloc[split_idx_1:split_idx_2, :].copy(), df.iloc[split_idx_2:, :].copy()
    train_set = pd.concat([train_set1,  train_set2], ignore_index=True)
    print(user_num, item_num)

    test_ur = get_ur(test_set)
    train1_ur = get_ur(train_set1)
    train2_ur = get_ur(train_set2)
    total_train_ur = get_ur(train_set)
    
    if args.method == 'mostpop' or args.method == 'itemknn':
        pred_file_path1 = f'./res/{args.dataset}/{args.method}/{args.dataset}_{args.prepro}_result_{args.method}_train.csv'
        pred_file_path2 = f'./res/{args.dataset}/{args.method}/{args.dataset}_{args.prepro}_result_{args.method}_train1.csv'
    else:
        pred_file_path1 = f'./res/{args.dataset}/{args.method}/{args.dataset}_{args.prepro}_{args.factors}_result_{args.method}_train.csv'
        pred_file_path2 = f'./res/{args.dataset}/{args.method}/{args.dataset}_{args.prepro}_{args.factors}_result_{args.method}_train1.csv'
    
    train1_ur_l = get_ur_l(train_set1)
    total_train_ur_l = get_ur_l(train_set)

    pred_set1 = pd.read_csv(pred_file_path1)
    pred_set2 = pd.read_csv(pred_file_path2)

    pred1 = get_model_pred(pred_set1)
    pred2 = get_model_pred(pred_set2)

    # initial candidate item pool
    item_pool = set(range(item_num))
    candidates_num = args.cand_num

    print('='*50, '\n')
    print("action space complete")
    
    user_set = set()
    train2_ur = get_ur(train_set2)
    for k, v in train2_ur.items():
        user_set.add(k)

    test_ucands = defaultdict(list)
    user_test_set = set()
    for k, v in test_ur.items():
        user_test_set.add(k)
        sample_num = candidates_num - len(v) if len(v) < candidates_num else 0
        sub_item_pool = item_pool - v - total_train_ur[k] # remove GT & interacted
        sample_num = min(len(sub_item_pool), sample_num)
        if sample_num == 0:
            samples = random.sample(v, candidates_num)
        else:
            samples = random.sample(sub_item_pool, sample_num)
            test_ucands[k] = list(v | set(samples))
    
    if args.method == 'Item2Vec' or args.method == 'mostpop' or args.method == 'itemknn':
        pre_model = torch.load(f'./tmp/{args.dataset}/Item2Vec/{args.prepro}_{args.factors}_Item2Vec_train.pt')
    else:
        pre_model = torch.load(f'./tmp/{args.dataset}/{args.method}/{args.prepro}_{args.factors}_{args.method}_train.pt')

    if args.pred_score:
        model = torch.load(f'./tmp/{args.dataset}/{args.method}/{args.prepro}_{args.factors}_{args.method}_train.pt')
    else:
        model = pre_model
    dqn = DQN(user_num, item_num, pre_model, args.n_actions, args.method, factor_num=args.factors, gpuid=args.gpu)
    print("=======model initial completed========")

    preds = {}
    epoch = 0
    total_ep_r = 0

    print("=====training=====")
    for user in tqdm(user_set):
        ep_r = 0
        ur = train1_ur_l[user][-5:]
        s = pad_ur(ur, item_num)
        recommend_item = []
        dqn.action_space = []
        dqn.candidate_actions = []
        dqn.create_action_space(pred1[user])
        # print(user)
        dqn.memory_counter = 0
        for t in range(50):
            a = dqn.choose_action(user, s, 1)
            recommend_item.append(a)
            dqn.update_action_space(a)
            s_ = get_next_state(s, a, train2_ur[user])
            r = get_reward(recommend_item, a, train2_ur[user], user, model, args.pred_score, args.method)
            dqn.store_transition(user, s, a, r, s_, dqn.action_space)
            ep_r += r
            if dqn.memory_counter > args.memory_capacity:
                dqn.learn(user)
            s = s_
        preds[user] = recommend_item
        gc.collect()
        epoch += 1
        total_ep_r += ep_r
        if epoch % 50 == 0:
            # print(total_ep_r / 50)
            total_ep_r = 0
    del preds
    
    print("=====testing=====")
    preds = {}
    user_test = set()
    for user in tqdm(user_test_set):
        ur = total_train_ur_l[user][-5:]
        s = pad_ur(ur, item_num)
        recommend_item = []
        if not user not in preds.keys():
            continue
        else:
            user_test.add(user)
        dqn.create_action_space(pred2[user])
        for t in range(20):
            a = dqn.choose_action(user, s, 0)
            recommend_item.append(a)
            dqn.update_action_space(a)
            s_ = get_next_state(s, a, test_ur[user])
            s = s_
        preds[user] = recommend_item

    u_binary = []
    u_result = []
    res = preds.copy()
    record = {}
    u_record = {}
    u_test = []
    for u in user_test:
        u_test.append(u)
        u_record[u] = [u] + res[u]
        u_result.append(u_record[u])
        preds[u] = [1 if i in test_ur[u] else 0 for i in preds[u]]
        record[u] = [u] + preds[u]
        u_binary.append(record[u])
    
    # process topN list and store result for reporting KPI
    print('Save metric@k result to res folder...')
    result_save_path = f'./res/{args.dataset}/{args.method}/'
    if not os.path.exists(result_save_path):
        os.makedirs(result_save_path)
    
    # save binary-interaction list to csv file
    pred_csv = pd.DataFrame(data=u_binary)
    pred_csv.to_csv(f'{result_save_path}{args.dataset}_rl_{args.n_actions}_{args.prepro}_{args.factors}_trd.csv', index=False)

    test_user = pd.DataFrame(data=u_result)
    test_user.to_csv(f'{result_save_path}{args.dataset}_rl_{args.n_actions}_{args.prepro}_{args.factors}_testuser_trd.csv', index=False)

    res = pd.DataFrame({'metric@K': ['pre', 'rec', 'hr', 'map', 'mrr', 'ndcg']})

    for k in [1, 5, 10]:
        if k > args.topk:
            continue
        tmp_preds = preds.copy()        
        tmp_preds = {key: rank_list[:k] for key, rank_list in tmp_preds.items()}

        pre_k = np.mean([precision_at_k(r, k) for r in tmp_preds.values()])
        rec_k = recall_at_k(tmp_preds, test_ur, u_test, k)
        hr_k = hr_at_k(tmp_preds, test_ur)
        map_k = map_at_k(tmp_preds.values())
        mrr_k = mrr_at_k(tmp_preds, k)
        ndcg_k = np.mean([ndcg_at_k(r, k) for r in tmp_preds.values()])

        if k == 10:
            print(f'Precision@{k}: {pre_k:.4f}')
            print(f'Recall@{k}: {rec_k:.4f}')
            print(f'HR@{k}: {hr_k:.4f}')
            print(f'MAP@{k}: {map_k:.4f}')
            print(f'MRR@{k}: {mrr_k:.4f}')
            print(f'NDCG@{k}: {ndcg_k:.4f}')

        res[k] = np.array([pre_k, rec_k, hr_k, map_k, mrr_k, ndcg_k])

    res.to_csv(f'{result_save_path}{args.dataset}_rl_result_{args.n_actions}_{args.prepro}_{args.factors}_trd.csv', 
               index=False)
    print('='* 20, ' Done ', '='*20)








    
    
        
# Copyright 2020-present, Pietro Buzzega, Matteo Boschini, Angelo Porrello, Davide Abati, Simone Calderara.
# All rights reserved.
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

import torch
import numpy as np
from typing import Tuple
from torchvision import transforms
from torch.utils.data import Dataset, DataLoader

def reservoir(num_seen_examples: int, buffer_size: int) -> int:
    """
    Reservoir sampling algorithm.
    :param num_seen_examples: the number of seen examples
    :param buffer_size: the maximum buffer size
    :return: the target index if the current image is sampled, else -1
    """
    if num_seen_examples < buffer_size:
        return num_seen_examples

    rand = np.random.randint(0, num_seen_examples + 1)
    if rand < buffer_size:
        return rand
    else:
        return -1


def ring(num_seen_examples: int, buffer_portion_size: int, task: int) -> int:
    return num_seen_examples % buffer_portion_size + task * buffer_portion_size

def fifo(num_seen_examples: int, buffer_size: int) -> int:
    return num_seen_examples % buffer_size


class StreamDataset(Dataset):
    
    def __init__(self, x, y, logits, seq_len, pred_len, sample_freq, fast_mode=True):
        self.x = x
        self.y = y
        self.logits = logits
        self.seq_len = seq_len
        self.pred_len = pred_len
        self.sample_freq = sample_freq
        self.fast_mode = fast_mode
    
    def __getitem__(self, index):
        s_begin = index * self.sample_freq
        s_end = s_begin + self.seq_len
        seq_x = self.x[0,s_begin:s_end]
        
        seq_y = self.y[0, s_end:s_end+self.pred_len]
        
        logits = self.logits[index]
        
        return seq_x, seq_y, logits
    
    def __len__(self):
        if self.fast_mode:
            return self.x.size(1) - self.seq_len
        
        return (self.x.size(1)-self.seq_len - self.pred_len)//self.sample_freq+1
            


class FastStreamBuffer:
    """
    The fast steam buffer of rehearsal method.
    """
    def __init__(self, buffer_size, seq_len, pred_len, device):
        self.buffer_size = buffer_size
        self.device = device
        self.seq_len = seq_len
        self.pred_len = pred_len
        
        self.x = None
        self.length = 0
    
    def add_data(self, x, pred):
        
        if self.x is None:
            self.x = x
            self.y = pred
            self.length = 0
        else:
            if self.length>=self.buffer_size:
                self.x = self.x[:,1:,:]
                
            self.x = torch.cat((self.x,x[:,-1:,:]),1)
            self.y = torch.cat((self.x,pred),1)
            self.length = self.x.size(1)-self.seq_len
        
        # print(self.x.shape, self.x_mark.shape, self.y_mark.shape, self.y.shape,'ddd')
        
    def get_data(self, size):

        dataset = StreamDataset(self.x, self.y, self.seq_len, self.pred_len)
        
        if size>len(dataset):
            size = len(dataset)
        
        data_loader = DataLoader(
            dataset,
            batch_size=size,
            shuffle=True,
            drop_last=False)
        
        return data_loader

    def clear(self):
        self.x = None
        self.length = 0
            
            
class SlowStreamBuffer:
    """
    The slow steam buffer of rehearsal method.
    """
    def __init__(self, buffer_size, seq_len, pred_len, sample_freq):
        self.buffer_size = buffer_size
        self.seq_len = seq_len
        self.pred_len = pred_len
        self.sample_freq = sample_freq
        
        self.x = None
        self.length = 0
        self.logits = []
    
    def add_data(self, x, logits):
        
        if self.x is None:
            self.x = x
            self.logits = [logits]
            self.length = 0
        else:
            if self.length>=self.buffer_size:
                self.x = self.x[:,self.sample_freq:,:]
                self.logits = self.logits[1:]
                
            self.x = torch.cat((self.x, x[:,-self.sample_freq:,:]),1)
            self.logits.append(logits)
            if self.x.size(1)-self.seq_len - self.pred_len>=0:
                self.length = (self.x.size(1)-self.seq_len - self.pred_len)//self.sample_freq+1
            else:
                self.length = 0
        
        # print(self.x.shape, len(self.logits), self.length,'ddd')
        
    def get_data(self, size):

        dataset = StreamDataset(self.x, self.x, self.logits, self.seq_len, self.pred_len, self.sample_freq, fast_mode=False)
        #print(self.x.size(), self.seq_len,self.pred_len , 1,';;;')
        if size>len(dataset):
            size = len(dataset)
        
        data_loader = DataLoader(
            dataset,
            batch_size=size,
            shuffle=True,
            drop_last=False)
        
        return data_loader
    
    def clear(self):
        self.x = None
        self.logits=[]
        self.length = 0
            


class Buffer:
    """
    The memory buffer of rehearsal method.
    """
    def __init__(self, buffer_size, device, n_tasks=1, mode='fifo'):
        assert mode in ['ring', 'reservoir', 'fifo']
        self.buffer_size = buffer_size
        self.device = device
        self.num_seen_examples = 0
        self.functional_index = eval(mode)
        if mode == 'ring':
            assert n_tasks is not None
            self.task_number = n_tasks
            self.buffer_portion_size = buffer_size // n_tasks
        self.attributes = ['examples', 'labels', 'logits', 'task_labels']

    def init_tensors(self, examples: torch.Tensor, labels: torch.Tensor,
                     logits: torch.Tensor, task_labels: torch.Tensor) -> None:
        """
        Initializes just the required tensors.
        :param examples: tensor containing the images
        :param labels: tensor containing the labels
        :param logits: tensor containing the outputs of the network
        :param task_labels: tensor containing the task labels
        """
        for attr_str in self.attributes:
            attr = eval(attr_str)
            if attr is not None and not hasattr(self, attr_str):
                #typ = torch.int64 if attr_str.endswith('els') else torch.float32
                typ = torch.float32
                setattr(self, attr_str, torch.zeros((self.buffer_size,
                        *attr.shape[1:]), dtype=typ, device=self.device))

    def add_data(self, examples, labels=None, logits=None, task_labels=None):
        """
        Adds the data to the memory buffer according to the reservoir strategy.
        :param examples: tensor containing the images
        :param labels: tensor containing the labels
        :param logits: tensor containing the outputs of the network
        :param task_labels: tensor containing the task labels
        :return:
        """
        if not hasattr(self, 'examples'):
            self.init_tensors(examples, labels, logits, task_labels)

        for i in range(examples.shape[0]):
            index = fifo(self.num_seen_examples, self.buffer_size)
            self.num_seen_examples += 1
            if index >= 0:
                self.examples[index] = examples[i].to(self.device)
                if labels is not None:
                    self.labels[index] = labels[i].to(self.device)
                if logits is not None:
                    self.logits[index] = logits[i].to(self.device)
                if task_labels is not None:
                    self.task_labels[index] = task_labels[i].to(self.device)

    def get_data(self, size: int, transform: transforms=None) -> Tuple:
        """
        Random samples a batch of size items.
        :param size: the number of requested items
        :param transform: the transformation to be applied (data augmentation)
        :return:
        """
        if size > min(self.num_seen_examples, self.examples.shape[0]):
            size = min(self.num_seen_examples, self.examples.shape[0])

        choice = np.random.choice(min(self.num_seen_examples, self.examples.shape[0]),
                                  size=size, replace=False)
        if transform is None: transform = lambda x: x
        ret_tuple = (torch.stack([transform(ee.cpu())
                            for ee in self.examples[choice]]).to(self.device),)
        for attr_str in self.attributes[1:]:
            if hasattr(self, attr_str):
                attr = getattr(self, attr_str)
                ret_tuple += (attr[choice],)

        return ret_tuple

    def is_empty(self) -> bool:
        """
        Returns true if the buffer is empty, false otherwise.
        """
        if self.num_seen_examples == 0:
            return True
        else:
            return False

    def get_all_data(self, transform: transforms=None) -> Tuple:
        """
        Return all the items in the memory buffer.
        :param transform: the transformation to be applied (data augmentation)
        :return: a tuple with all the items in the memory buffer
        """
        if transform is None: transform = lambda x: x
        ret_tuple = (torch.stack([transform(ee.cpu())
                            for ee in self.examples]).to(self.device),)
        for attr_str in self.attributes[1:]:
            if hasattr(self, attr_str):
                attr = getattr(self, attr_str)
                ret_tuple += (attr,)
        return ret_tuple

    def empty(self) -> None:
        """
        Set all the tensors to None.
        """
        for attr_str in self.attributes:
            if hasattr(self, attr_str):
                delattr(self, attr_str)
        self.num_seen_examples = 0



class BufferFIFO:
    """
    The memory buffer of rehearsal method.
    """
    def __init__(self, buffer_size, device, n_tasks=1, mode='reservoir'):
        assert mode in ['ring', 'reservoir']
        self.buffer_size = buffer_size
        self.device = device
        self.num_seen_examples = 0
        self.functional_index = eval(mode)
        if mode == 'ring':
            assert n_tasks is not None
            self.task_number = n_tasks
            self.buffer_portion_size = buffer_size // n_tasks
        self.attributes = ['losses',]

    def init_tensors(self, losses: torch.Tensor, labels: torch.Tensor,
                     logits: torch.Tensor, task_labels: torch.Tensor) -> None:
        """
        Initializes just the required tensors.
        :param examples: tensor containing the images
        """
        for attr_str in self.attributes:
            attr = eval(attr_str)
            if attr is not None and not hasattr(self, attr_str):
                #typ = torch.int64 if attr_str.endswith('els') else torch.float32
                typ = torch.float32
                setattr(self, attr_str, torch.zeros((self.buffer_size,
                        *attr.shape[1:]), dtype=typ, device=self.device))

    def add_data(self, losses, labels=None, logits=None, task_labels=None):
        """
        Adds the data to the memory buffer according to the reservoir strategy.
        :param losses: tensor containing the images
        :return:
        """
        if not hasattr(self, 'losses'):
            self.init_tensors(losses, labels, logits, task_labels)

       
        self.losses[self.num_seen_examples] = losses
        self.num_seen_examples += 1

    def get_data(self, size: int, transform: transforms=None) -> Tuple:
        """
        Random samples a batch of size items.
        :param size: the number of requested items
        :param transform: the transformation to be applied (data augmentation)
        :return:
        """
        if size > min(self.num_seen_examples, self.losses.shape[0]):
            size = min(self.num_seen_examples, self.losses.shape[0])
        return torch.mean(self.losses[self.num_seen_examples-size:self.num_seen_examples])

    def is_empty(self) -> bool:
        """
        Returns true if the buffer is empty, false otherwise.
        """
        if self.num_seen_examples == 0:
            return True
        else:
            return False

    def get_all_data(self, transform: transforms=None) -> Tuple:
        """
        Return all the items in the memory buffer.
        :param transform: the transformation to be applied (data augmentation)
        :return: a tuple with all the items in the memory buffer
        """
        if transform is None: transform = lambda x: x
        ret_tuple = (torch.stack([transform(ee.cpu())
                            for ee in self.examples]).to(self.device),)
        for attr_str in self.attributes[1:]:
            if hasattr(self, attr_str):
                attr = getattr(self, attr_str)
                ret_tuple += (attr,)
        return ret_tuple

    def empty(self) -> None:
        """
        Set all the tensors to None.
        """
        for attr_str in self.attributes:
            if hasattr(self, attr_str):
                delattr(self, attr_str)
        self.num_seen_examples = 0
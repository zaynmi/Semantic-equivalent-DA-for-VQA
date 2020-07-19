"""
Adapted from PyTorch's text library.
"""

import array
import os
import zipfile

import six
import torch
from six.moves.urllib.request import urlretrieve
from tqdm import tqdm
import numpy as np
import torch
import torch.nn as nn
import torch.nn.init as init
from torch.autograd import Variable
from torch.nn.utils.rnn import pack_padded_sequence

import config
from config import qa_path

class TextProcessor(nn.Module):
    def __init__(self, classes, embedding_features, lstm_features, drop=0.0, use_hidden=True, use_tanh=False, only_embed=False):
        super(TextProcessor, self).__init__()
        self.use_hidden = use_hidden # return last layer hidden, else return all the outputs for each words
        self.use_tanh = use_tanh
        self.only_embed = only_embed
        classes = list(classes)

        self.embed = nn.Embedding(len(classes)+1, embedding_features, padding_idx=len(classes))
        weight_init = torch.from_numpy(np.load(qa_path+'/glove6b_init_300d.npy'))
        assert weight_init.shape == (len(classes), embedding_features)
        # print('glove weight shape: ', weight_init.shape)
        self.embed.weight.data[:len(classes)] = weight_init
        # print('word embed shape: ', self.embed.weight.shape)
        
        self.drop = nn.Dropout(drop)

        if self.use_tanh:
            self.tanh = nn.Tanh()

        if not self.only_embed:
            self.lstm = nn.GRU(input_size=embedding_features,
                           hidden_size=lstm_features,
                           num_layers=1,
                           batch_first=not use_hidden,)

    def forward(self, q, q_len, pred_from_emb=None):
        if pred_from_emb is not None:
            self.embedded = pred_from_emb.requires_grad_()
        else:
            self.embedded = self.embed(q).requires_grad_()

        # embedded = self.embed(q)
        embedded = self.drop(self.embedded)

        if self.use_tanh:
            embedded = self.tanh(embedded)

        if self.only_embed:
            return embedded

        self.lstm.flatten_parameters()
        if self.use_hidden:
            packed = pack_padded_sequence(embedded, q_len, batch_first=True)
            _, hid = self.lstm(packed)
            return hid.squeeze(0)
        else:
            out, _ = self.lstm(embedded)
            return out


#embed_vecs = obj_edge_vectors(classes, wv_dim=embedding_features)
#self.embed.weight.data = embed_vecs.clone()
def obj_edge_vectors(names, wv_type='glove.6B', wv_dir=qa_path, wv_dim=300):
    wv_dict, wv_arr, wv_size = load_word_vectors(wv_dir, wv_type, wv_dim)

    vectors = torch.Tensor(len(names), wv_dim)
    vectors.normal_(0,1)
    failed_token = []
    for i, token in enumerate(names):
        wv_index = wv_dict.get(token, None)
        if wv_index is not None:
            vectors[i] = wv_arr[wv_index]
        else:
            # Try the longest word (hopefully won't be a preposition
            lw_token = sorted(token.split(' '), key=lambda x: len(x), reverse=True)[0]
            #print("{} -> {} ".format(token, lw_token))
            wv_index = wv_dict.get(lw_token, None)
            if wv_index is not None:
                vectors[i] = wv_arr[wv_index]
            else:
                failed_token.append(token)
    if (len(failed_token) > 0):
        print('Num of failed tokens: ', len(failed_token))
        #print(failed_token)
    return vectors

URL = {
        'glove.42B': 'http://nlp.stanford.edu/data/glove.42B.300d.zip',
        'glove.840B': 'http://nlp.stanford.edu/data/glove.840B.300d.zip',
        'glove.twitter.27B': 'http://nlp.stanford.edu/data/glove.twitter.27B.zip',
        'glove.6B': 'http://nlp.stanford.edu/data/glove.6B.zip',
        }


def load_word_vectors(root, wv_type, dim):
    """Load word vectors from a path, trying .pt, .txt, and .zip extensions."""
    if isinstance(dim, int):
        dim = str(dim) + 'd'
    fname = os.path.join(root, wv_type + '.' + dim)
    if os.path.isfile(fname + '.pt'):
        fname_pt = fname + '.pt'
        print('loading word vectors from', fname_pt)
        return torch.load(fname_pt)
    if os.path.isfile(fname + '.txt'):
        fname_txt = fname + '.txt'
        cm = open(fname_txt, 'rb')
        cm = [line for line in cm]
    elif os.path.basename(wv_type) in URL:
        url = URL[wv_type]
        print('downloading word vectors from {}'.format(url))
        filename = os.path.basename(fname)
        if not os.path.exists(root):
            os.makedirs(root)
        with tqdm(unit='B', unit_scale=True, miniters=1, desc=filename) as t:
            fname, _ = urlretrieve(url, fname, reporthook=reporthook(t))
            with zipfile.ZipFile(fname, "r") as zf:
                print('extracting word vectors into {}'.format(root))
                zf.extractall(root)
        if not os.path.isfile(fname + '.txt'):
            raise RuntimeError('no word vectors of requested dimension found')
        return load_word_vectors(root, wv_type, dim)
    else:
        raise RuntimeError('unable to load word vectors')

    wv_tokens, wv_arr, wv_size = [], array.array('d'), None
    if cm is not None:
        for line in tqdm(range(len(cm)), desc="loading word vectors from {}".format(fname_txt)):
            entries = cm[line].strip().split(b' ')
            word, entries = entries[0], entries[1:]
            if wv_size is None:
                wv_size = len(entries)
            try:
                if isinstance(word, six.binary_type):
                    word = word.decode('utf-8')
            except:
                print('non-UTF8 token', repr(word), 'ignored')
                continue
            wv_arr.extend(float(x) for x in entries)
            wv_tokens.append(word)

    wv_dict = {word: i for i, word in enumerate(wv_tokens)}
    wv_arr = torch.Tensor(wv_arr).view(-1, wv_size)
    ret = (wv_dict, wv_arr, wv_size)
    torch.save(ret, fname + '.pt')
    return ret

def reporthook(t):
    """https://github.com/tqdm/tqdm"""
    last_b = [0]

    def inner(b=1, bsize=1, tsize=None):
        """
        b: int, optionala
        Number of blocks just transferred [default: 1].
        bsize: int, optional
        Size of each block (in tqdm units) [default: 1].
        tsize: int, optional
        Total size (in tqdm units). If [default: None] remains unchanged.
        """
        if tsize is not None:
            t.total = tsize
        t.update((b - last_b[0]) * bsize)
        last_b[0] = b
    return inner

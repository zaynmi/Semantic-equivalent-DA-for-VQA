import json
import os
import os.path
import re

import _pickle as cPickle
from PIL import Image
import h5py
import torch
import torch.utils.data as data
import torchvision.transforms as transforms
import numpy as np

import config
from . import utils


preloaded_vocab = None


def get_loader(train=False, val=False, test=False, trainval=False, sea=False, frac=1, iq=False, vqacp=False):
    """ Returns a data loader for the desired split """
    split = VQA(
        utils.path_for(train=train, val=val, test=test, trainval=trainval, question=True, iq=iq, vqacp=vqacp),
        utils.path_for(train=train, val=val, test=test, trainval=trainval, answer=True, iq=iq, vqacp=vqacp),
        config.preprocessed_trainval_path if not test else config.preprocessed_test_path,
        utils.path_for(train=train, val=val, test=test, trainval=trainval, question=True, sea=sea, iq=iq),
        answerable_only=train or trainval,
        frac=frac,
        dummy_answers=test,
    )
    loader = torch.utils.data.DataLoader(
        split,
        batch_size=64 if config.model_type == 'ban' and val else config.batch_size,
        shuffle=train or trainval,  # only shuffle the data in training
        pin_memory=True,
        num_workers=config.data_workers,
        collate_fn=collate_fn,
    )
    return loader


def collate_fn(batch):
    # put question lengths in descending order so that we can use packed sequences later
    batch.sort(key=lambda x: x[-1], reverse=True)
    return data.dataloader.default_collate(batch)


class VQA(data.Dataset):
    """ VQA dataset, open-ended """
    def __init__(self, questions_path, answers_path, image_features_path, questions_adv_path=None, answerable_only=False, frac=1, dummy_answers=False):
        super(VQA, self).__init__()
        with open(questions_path, 'r') as fd:
            questions_json = json.load(fd)
        with open(answers_path, 'r') as fd:
            answers_json = json.load(fd)
        if 'adv' in questions_adv_path:
            with open(questions_adv_path, 'r') as fd:
                questions_adv_json = json.load(fd)
        if preloaded_vocab:
            vocab_json = preloaded_vocab
        else:
            with open(config.vocabulary_path, 'r') as fd:
                vocab_json = json.load(fd)
            word2idx, idx2word = cPickle.load(open(config.glove_index, 'rb'))
            vocab_json['question'] = word2idx

        self.question_ids = [q['question_id'] for q in questions_json['questions']]

        # vocab
        self.vocab = vocab_json
        self.token_to_index = self.vocab['question']
        self.answer_to_index = self.vocab['answer']

        # q and a
        self.q_id = [q['question_id'] for q in questions_json['questions']]
        self.question_str = [q['question'] for q in questions_json['questions']]   # for sea
        self.questions = list(prepare_questions(questions_json, self.q_id))
        self.questions_adv = None
        if 'adv' in questions_adv_path:
            self.questions_adv = list(prepare_questions(questions_adv_json, self.q_id))
            self.questions_adv = [self.encode_question(q) for q in self.questions_adv]
        self.answers = list(prepare_answers(answers_json, self.q_id))
        self.questions = [self.encode_question(q) for q in self.questions]
        self.answers = [self._encode_answers(a) for a in self.answers]

        # v
        self.image_features_path = image_features_path
        self.coco_id_to_index = self._create_coco_id_to_index()
        self.coco_ids = [q['image_id'] for q in questions_json['questions']]

        self.dummy_answers= dummy_answers

        # only use questions that have at least one answer?
        self.answerable_only = answerable_only
        if self.answerable_only:
            self.answerable = self._find_answerable(not self.answerable_only)
            self.answerable = self.answerable[:int(len(self.answerable) * frac)]
            
    @property
    def max_question_length(self):
        if not hasattr(self, '_max_length'):
            data_max_length = max(map(len, self.questions))
            self._max_length = min(config.max_q_length, data_max_length)
        return self._max_length

    @property
    def num_tokens(self):
        return len(self.token_to_index)

    def _create_coco_id_to_index(self):
        """ Create a mapping from a COCO image id into the corresponding index into the h5 file """
        with h5py.File(self.image_features_path, 'r') as features_file:
            coco_ids = features_file['ids'][()]
        coco_id_to_index = {id: i for i, id in enumerate(coco_ids)}
        return coco_id_to_index

    def _check_integrity(self, questions, answers):
        """ Verify that we are using the correct data """
        qa_pairs = list(zip(questions['questions'], answers['annotations']))
        assert all(q['question_id'] == a['question_id'] for q, a in qa_pairs), 'Questions not aligned with answers'
        assert all(q['image_id'] == a['image_id'] for q, a in qa_pairs), 'Image id of question and answer don\'t match'
        assert questions['data_type'] == answers['data_type'], 'Mismatched data types'
        assert questions['data_subtype'] == answers['data_subtype'], 'Mismatched data subtypes'

    def _find_answerable(self, count=False):
        """ Create a list of indices into questions that will have at least one answer that is in the vocab """
        answerable = []
        if count:
            number_indices = torch.LongTensor([self.answer_to_index[str(i)] for i in range(0, 8)])
        for i, answers in enumerate(self.answers):
            # store the indices of anything that is answerable
            if count:
                answers = answers[number_indices]
            answer_has_index = len(answers.nonzero()) > 0
            if answer_has_index:
                answerable.append(i)
        return answerable

    def encode_question(self, question):
        """ Turn a question into a vector of indices and a question length """
        vec = torch.zeros(self.max_question_length).long().fill_(self.num_tokens)
        for i, token in enumerate(question):
            if i >= self.max_question_length:
                break
            index = self.token_to_index.get(token, self.num_tokens - 1)
            vec[i] = index
        return vec, min(len(question), self.max_question_length)

    def _encode_answers(self, answers):
        """ Turn an answer into a vector """
        # answer vec will be a vector of answer counts to determine which answers will contribute to the loss.
        # this should be multiplied with 0.1 * negative log-likelihoods that a model produces and then summed up
        # to get the loss that is weighted by how many humans gave that answer
        answer_vec = torch.zeros(len(self.answer_to_index))
        for answer in answers:
            index = self.answer_to_index.get(answer)
            if index is not None:
                answer_vec[index] += 1
        return answer_vec

    def _load_image(self, image_id):
        """ Load an image """
        if not hasattr(self, 'features_file'):
            # Loading the h5 file has to be done here and not in __init__ because when the DataLoader
            # forks for multiple works, every child would use the same file object and fail
            # Having multiple readers using different file objects is fine though, so we just init in here.
            self.features_file = h5py.File(self.image_features_path, 'r')
        index = self.coco_id_to_index[image_id]
        img = self.features_file['features'][index]
        boxes = self.features_file['boxes'][index]
        widths = self.features_file['widths'][index]
        heights = self.features_file['heights'][index]
        obj_mask = (img.sum(0) > 0).astype(int)
        return torch.from_numpy(img).transpose(0,1), torch.from_numpy(boxes).transpose(0,1), torch.from_numpy(obj_mask), widths, heights

    def __getitem__(self, item):
        if self.answerable_only:
            item = self.answerable[item]
        q, q_length = self.questions[item]
        q_adv = 0
        q_adv_mask = 0
        q_adv_length = 0
        if self.questions_adv is not None:
            q_adv, q_adv_length = self.questions_adv[item]
            q_adv_mask = torch.from_numpy((np.arange(self.max_question_length) < q_adv_length).astype(int)).float()
        q_str = self.question_str[item]
        q_mask = torch.from_numpy((np.arange(self.max_question_length) < q_length).astype(int))
        if not self.dummy_answers:
            a = self.answers[item]
        else:
            # just return a dummy answer, it's not going to be used anyway
            a = 0
        image_id = self.coco_ids[item]
        q_id = self.q_id[item]
        v, b, obj_mask, width, height = self._load_image(image_id)
        # since batches are re-ordered for PackedSequence's, the original question order is lost
        # we return `item` so that the order of (v, q, a) triples can be restored if desired
        # without shuffling in the dataloader, these will be in the order that they appear in the q and a json's.
        if config.normalize_box:
            assert b.shape[1] == 4
            b[:, 0] = b[:, 0] / float(width)
            b[:, 1] = b[:, 1] / float(height)
            b[:, 2] = b[:, 2] / float(width)
            b[:, 3] = b[:, 3] / float(height)
        
        return v, q, q_adv, q_str, a, b, item, obj_mask.float(), q_mask.float(), q_adv_mask, image_id, q_id, q_adv_length, q_length

    def __len__(self):
        if self.answerable_only:
            return len(self.answerable)
        else:
            return len(self.questions)


# this is used for normalizing questions
_special_chars = re.compile('[^a-z0-9 ]*')

# these try to emulate the original normalization scheme for answers
_period_strip = re.compile(r'(?!<=\d)(\.)(?!\d)')
_comma_strip = re.compile(r'(\d)(,)(\d)')
_punctuation_chars = re.escape(r';/[]"{}()=+\_-><@`,?!')
_punctuation = re.compile(r'([{}])'.format(re.escape(_punctuation_chars)))
_punctuation_with_a_space = re.compile(r'(?<= )([{0}])|([{0}])(?= )'.format(_punctuation_chars))


def prepare_questions(questions_json, q_id):
    """ Tokenize and normalize questions from a given question json in the usual VQA format. """
    questions = [q['question'] for q in questions_json['questions']]
    #ques_dict = {}
    #for q in questions_json['questions']:
    #    ques_dict[q['question_id']] = q
   # questions = [ques_dict[i]['question'] for i in q_id]
    for question in questions:
        question = question.lower()[:-1]
        question = _special_chars.sub('', question)
        yield question.split(' ')

def prepare_questions_from_para(paraphrases):
    for paraphrase in paraphrases:
        question = paraphrase[0].lower()[:-2]
        question = _special_chars.sub('', question)
        yield question.split(' ')

def prepare_answers(answers_json, q_id):
    """ Normalize answers from a given answer json in the usual VQA format. """
    answers = [[a['answer'] for a in ans_dict['answers']] for ans_dict in answers_json['annotations']]
   # new_ans = {}
    #for ans_dict in answers_json['annotations']:
    #    new_ans[ans_dict['question_id']] = ans_dict
    #answers = [[a['answer'] for a in new_ans[i]['answers']] for i in q_id]
    # The only normalization that is applied to both machine generated answers as well as
    # ground truth answers is replacing most punctuation with space (see [0] and [1]).
    # Since potential machine generated answers are just taken from most common answers, applying the other
    # normalizations is not needed, assuming that the human answers are already normalized.
    # [0]: http://visualqa.org/evaluation.html
    # [1]: https://github.com/VT-vision-lab/VQA/blob/3849b1eae04a0ffd83f56ad6f70ebd0767e09e0f/PythonEvaluationTools/vqaEvaluation/vqaEval.py#L96

    def process_punctuation(s):
        # the original is somewhat broken, so things that look odd here might just be to mimic that behaviour
        # this version should be faster since we use re instead of repeated operations on str's
        if _punctuation.search(s) is None:
            return s
        s = _punctuation_with_a_space.sub('', s)
        if re.search(_comma_strip, s) is not None:
            s = s.replace(',', '')
        s = _punctuation.sub(' ', s)
        s = _period_strip.sub('', s)
        return s.strip()

    for answer_list in answers:
        yield list(map(process_punctuation, answer_list))


class CocoImages(data.Dataset):
    """ Dataset for MSCOCO images located in a folder on the filesystem """
    def __init__(self, path, transform=None):
        super(CocoImages, self).__init__()
        self.path = path
        self.id_to_filename = self._find_images()
        self.sorted_ids = sorted(self.id_to_filename.keys())  # used for deterministic iteration order
        print('found {} images in {}'.format(len(self), self.path))
        self.transform = transform

    def _find_images(self):
        id_to_filename = {}
        for filename in os.listdir(self.path):
            if not filename.endswith('.jpg'):
                continue
            id_and_extension = filename.split('_')[-1]
            id = int(id_and_extension.split('.')[0])
            id_to_filename[id] = filename
        return id_to_filename

    def __getitem__(self, item):
        id = self.sorted_ids[item]
        path = os.path.join(self.path, self.id_to_filename[id])
        img = Image.open(path).convert('RGB')

        if self.transform is not None:
            img = self.transform(img)
        return id, img

    def __len__(self):
        return len(self.sorted_ids)

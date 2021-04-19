import gc
import glob
import hashlib
import itertools
import json
import os
import random
import re
import math
import subprocess
from collections import Counter
from os.path import join as pjoin

import torch
from multiprocess import Pool

from others.logging import logger
from others.tokenization import BertTokenizer
from transformers import RobertaTokenizer
from pytorch_transformers import XLNetTokenizer

from others.utils import clean
from prepro.utils import _get_word_ngrams

import xml.etree.ElementTree as ET
import pandas as pd
import time
from datetime import datetime
from tqdm import tqdm

nyt_remove_words = ["photo", "graph", "chart", "map", "table", "drawing"]


def recover_from_corenlp(s):
    s = re.sub(r' \'{\w}', '\'\g<1>', s)
    s = re.sub(r'\'\' {\w}', '\'\'\g<1>', s)


def clean_json(json_dict):
    #
    # how about bib? they also indicate what the paper is about in general
    #
    title = json_dict['metadata']['title']
    text = ''

    for p in json_dict['body_text']:
        if p['section'] == 'Pre-publication history':
            continue
        p_text = p['text'].strip()
        p_text = re.sub('\[[\d\s,]+?\]', '', p_text)
        p_text = re.sub('\(Table \d+?\)', '', p_text)
        p_text = re.sub('\(Fig. \d+?\)', '', p_text)
        text += '{:s}\n'.format(p_text)

    return {'title': title, 'text': text}

def load_json(f_main, f_abs, f_tag):
    with open(f_main, 'r') as f:
        json_main = json.load(f)
    with open(f_abs, 'r') as f:
        json_abs = json.load(f)

    src_sent_tokens = [
        list(t['word'].lower() for t in sent['tokens'])
        for sent in json_main['sentences']]
    if not src_sent_tokens:
        return None, None, None
    else:
        tgt_sent_tokens = [
        list(t['word'].lower() for t in sent['tokens'])
        for sent in json_abs['sentences']]

        with open(f_tag, 'r') as f:
            json_tag = json.load(f)
        tag_tokens = []
        tag_tags = []
        sent_lengths = [len(val) for val in src_sent_tokens]
        count = 0
        offset = 0
        temp_doc_len = len(json_tag)
        while offset < temp_doc_len:
            present_sent_len = sent_lengths[count]
            sent_tokens = json_tag[offset:offset + present_sent_len]
            try:
                assert [val.lower() for _, val in sent_tokens] == src_sent_tokens[count]
            except AssertionError as e:
                print(src_sent_tokens[count])
                print([val.lower() for _, val in sent_tokens])
        #assert [val.lower() for _, val in sent_tokens] == src_sent_tokens[count]
            offset += present_sent_len
            assert offset <= temp_doc_len
        #tag_tokens.append([val.lower() for _, val in sent_tokens])
            temp=[]
            for val, t in sent_tokens:
                if ' ' in t:
                    s = t.split()
                    align_tag = len(s)*[val]
                    temp += align_tag
                else:
                    temp.append(val)
            tag_tags.append(temp)
            count += 1
    #assert tag_tokens == src_sent_tokens

        tags = tag_tags
        src = [clean(' '.join(tokens)).split() for tokens in src_sent_tokens]
        for i, val in enumerate(src):
            assert len(val) == len(tags[i])

        tgt = [clean(' '.join(tokens)).split() for tokens in tgt_sent_tokens]
        return src, tgt, tags

def load_xml(p):
    tree = ET.parse(p)
    root = tree.getroot()
    title, byline, abs, paras = [], [], [], []
    title_node = list(root.iter('hedline'))
    if (len(title_node) > 0):
        try:
            title = [p.text.lower().split() for p in list(title_node[0].iter('hl1'))][0]
        except:
            print(p)

    else:
        return None, None
    byline_node = list(root.iter('byline'))
    byline_node = [n for n in byline_node if n.attrib['class'] == 'normalized_byline']
    if (len(byline_node) > 0):
        byline = byline_node[0].text.lower().split()
    abs_node = list(root.iter('abstract'))
    if (len(abs_node) > 0):
        try:
            abs = [p.text.lower().split() for p in list(abs_node[0].iter('p'))][0]
        except:
            print(p)

    else:
        return None, None
    abs = ' '.join(abs).split(';')
    abs[-1] = abs[-1].replace('(m)', '')
    abs[-1] = abs[-1].replace('(s)', '')

    for ww in nyt_remove_words:
        abs[-1] = abs[-1].replace('(' + ww + ')', '')
    abs = [p.split() for p in abs]
    abs = [p for p in abs if len(p) > 2]

    for doc_node in root.iter('block'):
        att = doc_node.get('class')
        # if(att == 'abstract'):
        #     abs = [p.text for p in list(f.iter('p'))]
        if (att == 'full_text'):
            paras = [p.text.lower().split() for p in list(doc_node.iter('p'))]
            break
    if (len(paras) > 0):
        if (len(byline) > 0):
            paras = [title + ['[unused3]'] + byline + ['[unused4]']] + paras
        else:
            paras = [title + ['[unused3]']] + paras

        return paras, abs
    else:
        return None, None


def tokenize(args):
    stories_dir = os.path.abspath(args.raw_path)
    tokenized_stories_dir = os.path.abspath(args.save_path)
    meta_path = os.path.join(stories_dir, 'metadata.csv')
    pmc_dir = os.path.join(stories_dir, 'document_parses', 'pmc_json')
    txt_dir = os.path.join(stories_dir, 'document_parses', 'txt_json')
    print('... Loading PMC data from {}'.format(pmc_dir))

    with open(meta_path, 'r') as f:
        df = pd.read_csv(meta_path, sep=',', error_bad_lines=False, index_col=False, dtype='unicode')
        len_before = len(df)
        # skip papers without abstract (more later)
        df = df[df.abstract.astype(bool)]

        files_count = len(df)
        files_count_real = 0

        print('Total files: {}'.format(len_before))
        print('Total files with abstract: {}'.format(files_count))
        print('Abstract files: {}/{}, ({}%)'.format(files_count, len_before, files_count / len_before * 100))

        start = time.time()
        print('... (1) Processing file into readable format for tokenizer...')

        with tqdm(total=files_count) as pbar:
            for i, row in df.iterrows():
                pbar.update(1)
                if not isinstance(row['abstract'], str):
                    continue
                pid = row['pmcid']
                pubtime = row['publish_time']
                # pubtime = datetime.strptime(row['publish_time'], '%Y-%m-%d').timestamp()
                ppath = os.path.join(pmc_dir, '{}.xml.json'.format(pid))
                if not os.path.isfile(ppath):
                    continue
                with open(ppath, 'r') as fi:
                    json_dict = json.load(fi)
                    dict = clean_json(json_dict)
                    tpath = os.path.join(txt_dir, '{}-{}.txt'.format(pubtime, pid))
                    tpath_abs = os.path.join(txt_dir, '{}-{}.abs.txt'.format(pubtime, pid))
                    with open(tpath, 'w') as fil:
                        fil.write(dict['text'])
                    with open(tpath_abs, 'w') as fil:
                        fil.write(row['abstract'])
                files_count_real += 1
            pbar.close()

        end = time.time()
        print('Real count for files with abstract: {} ({}%)'.format(files_count_real,files_count_real / len_before * 100))
        print('... Ending (1), time elapsed {}'.format(end - start))

    print("Preparing to tokenize %s to %s..." % (stories_dir, tokenized_stories_dir))
    stories = os.listdir(stories_dir)
    # make IO list file
    print("Making list of files to tokenize...")
    with open('mapping_for_corenlp.txt', 'w') as fi:
        for fname in os.listdir(txt_dir):
            fpath = os.path.join(txt_dir, fname)
            fi.write('{}\n'.format(fpath))

    command = ['java', 'edu.stanford.nlp.pipeline.StanfordCoreNLP', '-annotators', 'tokenize,ssplit',
               '-ssplit.newlineIsSentenceBreak', 'always', '-filelist', 'mapping_for_corenlp.txt', '-outputFormat',
               'json', '-outputDirectory', tokenized_stories_dir]

    print("Tokenizing %i files in %s and saving in %s..." % (len(stories), stories_dir, tokenized_stories_dir))
    subprocess.call(command)
    print("Stanford CoreNLP Tokenizer has finished.")
    os.remove("mapping_for_corenlp.txt")

    # Check that the tokenized stories directory contains the same number of files as the original directory
    num_orig = len(os.listdir(txt_dir))
    num_tokenized = len(os.listdir(tokenized_stories_dir))
    if num_orig != num_tokenized:
        raise Exception(
            "The tokenized stories directory %s contains %i files, but it should contain the same number as %s (which has %i files). Was there an error during tokenization?" % (
                tokenized_stories_dir, num_tokenized, stories_dir, num_orig))
    print("Successfully finished tokenizing %s to %s.\n" % (stories_dir, tokenized_stories_dir))

def cal_rouge(evaluated_ngrams, reference_ngrams):
    reference_count = len(reference_ngrams)
    evaluated_count = len(evaluated_ngrams)

    overlapping_ngrams = evaluated_ngrams.intersection(reference_ngrams)
    overlapping_count = len(overlapping_ngrams)

    if evaluated_count == 0:
        precision = 0.0
    else:
        precision = overlapping_count / evaluated_count

    if reference_count == 0:
        recall = 0.0
    else:
        recall = overlapping_count / reference_count

    f1_score = 2.0 * ((precision * recall) / (precision + recall + 1e-8))
    return {"f": f1_score, "p": precision, "r": recall}


def greedy_selection(doc_sent_list, abstract_sent_list, summary_size):
    def _rouge_clean(s):
        return re.sub(r'[^a-zA-Z0-9 ]', '', s)

    max_rouge = 0.0
    abstract = sum(abstract_sent_list, [])
    abstract = _rouge_clean(' '.join(abstract)).split()
    sents = [_rouge_clean(' '.join(s)).split() for s in doc_sent_list]
    evaluated_1grams = [_get_word_ngrams(1, [sent]) for sent in sents]
    reference_1grams = _get_word_ngrams(1, [abstract])
    evaluated_2grams = [_get_word_ngrams(2, [sent]) for sent in sents]
    reference_2grams = _get_word_ngrams(2, [abstract])

    selected = []
    for s in range(summary_size):
        cur_max_rouge = max_rouge
        cur_id = -1
        for i in range(len(sents)):
            if (i in selected):
                continue
            c = selected + [i]
            candidates_1 = [evaluated_1grams[idx] for idx in c]
            candidates_1 = set.union(*map(set, candidates_1))
            candidates_2 = [evaluated_2grams[idx] for idx in c]
            candidates_2 = set.union(*map(set, candidates_2))
            rouge_1 = cal_rouge(candidates_1, reference_1grams)['f']
            rouge_2 = cal_rouge(candidates_2, reference_2grams)['f']
            rouge_score = rouge_1 + rouge_2
            if rouge_score > cur_max_rouge:
                cur_max_rouge = rouge_score
                cur_id = i
        if (cur_id == -1):
            return selected
        selected.append(cur_id)
        max_rouge = cur_max_rouge

    return sorted(selected)


def hashhex(s):
    """Returns a heximal formated SHA1 hash of the input string."""
    h = hashlib.sha1()
    h.update(s.encode('utf-8'))
    return h.hexdigest()


class BertData():
    def __init__(self, args):
        self.args = args
        self.tokenizer = RobertaTokenizer.from_pretrained("roberta-base")

        #BertTokenizer.from_pretrained('bert-base-uncased', do_lower_case=True)

        self.sep_token = '<s>'
        self.cls_token = '</s>'
        self.pad_token = '<pad>'
        self.tgt_bos = '<s>'
        self.tgt_eos = '</s>'
        self.tgt_sent_split = '<s>'
        self.sep_vid = self.tokenizer.vocab[self.sep_token]
        self.cls_vid = self.tokenizer.vocab[self.cls_token]
        self.pad_vid = self.tokenizer.vocab[self.pad_token]
        self.tgt_bos_vid = self.tokenizer.vocab[self.tgt_bos]
        self.tgt_eos_vid = self.tokenizer.vocab[self.tgt_eos]
        self.tgt_sent_split_vid = self.tokenizer.vocab[self.tgt_sent_split]

    def preprocess(self, src, tgt, sent_labels, use_bert_basic_tokenizer=False, is_test=False):

        if ((not is_test) and len(src) == 0):
            return None

        original_src_txt = [' '.join(s) for s in src]

        idxs = [i for i, s in enumerate(src) if (len(s) > self.args.min_src_ntokens_per_sent)]

        _sent_labels = [0] * len(src)
        for l in sent_labels:
            _sent_labels[l] = 1

        src = [src[i][:self.args.max_src_ntokens_per_sent] for i in idxs]
        sent_labels = [_sent_labels[i] for i in idxs]
        src = src[:self.args.max_src_nsents]
        sent_labels = sent_labels[:self.args.max_src_nsents]

        if ((not is_test) and len(src) < self.args.min_src_nsents):
            return None

        src_txt = [' '.join(sent) for sent in src]
        text = ' {} {} '.format(self.sep_token, self.cls_token).join(src_txt)
        src_subtokens = self.tokenizer.tokenize(text)

        src_subtokens = [self.cls_token] + src_subtokens + [self.sep_token]
        src_subtoken_idxs = self.tokenizer.convert_tokens_to_ids(src_subtokens)
        _segs = [-1] + [i for i, t in enumerate(src_subtoken_idxs) if t == self.sep_vid]
        segs = [_segs[i] - _segs[i - 1] for i in range(1, len(_segs))]
        segments_ids = []
        for i, s in enumerate(segs):
            if (i % 2 == 0):
                segments_ids += s * [0]
            else:
                segments_ids += s * [1]
        cls_ids = [i for i, t in enumerate(src_subtoken_idxs) if t == self.cls_vid]
        sent_labels = sent_labels[:len(cls_ids)]

        tgt_subtokens_str = '<s>' + ' </s>'.join(
            [' '.join(self.tokenizer.tokenize(' '.join(tt), use_bert_basic_tokenizer=use_bert_basic_tokenizer)) for tt in tgt]) + ' </s>'
        tgt_subtoken = tgt_subtokens_str.split()[:self.args.max_tgt_ntokens]
        if ((not is_test) and len(tgt_subtoken) < self.args.min_tgt_ntokens):
            return None

        tgt_subtoken_idxs = self.tokenizer.convert_tokens_to_ids(tgt_subtoken)

        tgt_txt = '<q>'.join([' '.join(tt) for tt in tgt])
        src_txt = [original_src_txt[i] for i in idxs]

        return src_subtoken_idxs, sent_labels, tgt_subtoken_idxs, segments_ids, cls_ids, src_txt, tgt_txt

class PicoAdapterData():
    def __init__(self, args):
        self.args = args
        self.tokenizer = RobertaTokenizer.from_pretrained("roberta-base")

        #BertTokenizer.from_pretrained('bert-base-uncased', do_lower_case=True)

        self.sep_token = '<s>'
        self.cls_token = '</s>'
        self.pad_token = '<pad>'
        self.tgt_bos = '<s>'
        self.tgt_eos = '</s>'
        self.tgt_sent_split = '<s>'
        self.mask_token = '<mask>'
        self.sep_vid = self.tokenizer.convert_tokens_to_ids(self.sep_token)
        self.cls_vid = self.tokenizer.convert_tokens_to_ids(self.cls_token)
        self.pad_vid = self.tokenizer.convert_tokens_to_ids(self.pad_token)
        self.tgt_bos_vid = self.tokenizer.convert_tokens_to_ids(self.tgt_bos)
        self.tgt_eos_vid = self.tokenizer.convert_tokens_to_ids(self.tgt_eos)
        self.tgt_sent_split_vid = self.tokenizer.convert_tokens_to_ids(self.tgt_sent_split)
        self.mask_vid = self.tokenizer.convert_tokens_to_ids(self.mask_token)

    def preprocess(self, src, tag, is_test=False):

        if ((not is_test) and len(src) == 0):
            return None

        original_src_txt = [' '.join(s) for s in src]

        idxs = [i for i, s in enumerate(src) if (len(s) > self.args.min_src_ntokens_per_sent)]

        src = [src[i][:self.args.max_src_ntokens_per_sent] for i in idxs]
        tag = [tag[i][:self.args.max_src_ntokens_per_sent] for i in idxs]
        src = src[:self.args.max_src_nsents]
        tag = tag[:self.args.max_src_nsents]

        if ((not is_test) and len(src) < self.args.min_src_nsents):
            return None

        src_txt = [' '.join(sent) for sent in src]
        text = ' {} {} '.format(self.sep_token, self.cls_token).join(src_txt)
        tags = []
        for tag_list in tag:
            for t in tag_list:
                tags.append(t)
            tags.append('o')
            tags.append('o')

        count = 0
        annotations = []
        temp_dict = {}
        temp_str = ''
        for str in text:
            if str != ' ':
                temp_str += str
            else:
                temp_dict['text'] = temp_str
        #        temp_dict['start'] = start
        #        temp_dict['end'] = start+len(temp_str)
                temp_dict['label'] = tags[count]
        #        annotations.append(temp_dict)
        #         start = temp_dict['end']+2
                temp_dict = {}
                temp_str = ''
                count += 1
        temp_dict['text'] = temp_str
        temp_dict['label'] = tags[count]
        annotations.append(temp_dict)

        src_subtokens = self.tokenizer.tokenize(text)
        aligned_labels = ["o"] * len(src_subtokens)
        count = 0
        for anno in annotations:
            ano_text = anno['text']
            token_ix = self.tokenizer.tokenize(ano_text)
            for i in range(len(token_ix)):
                aligned_labels[count] = anno['label']
                count += 1
        src_subtokens = [self.cls_token] + src_subtokens + [self.sep_token]
        src_labels = []
        src_labels[0] = 'o'
        src_labels += aligned_labels
        src_labels[0] += 'o'
        src_subtoken_idxs = self.tokenizer.convert_tokens_to_ids(src_subtokens)
        tag_dict = {'o':0, "I-INT":1, "I-PAR": 2, "I-OUT": 3}
        src_tag_idx = [tag_dict[tag] for tag in src_labels]
        mask_label = []
        for i, tag in enumerate(src_labels):
            if tag == "o":
                mask_label.append(0.0)
            else:
                src_subtoken_idxs[i] = self.mask_vid
                mask_label.append(1.0)

        return src_subtoken_idxs, src_tag_idx, mask_label

def format_to_bert(args):
    print('... (5) Converting data to BERT data... this will take a while')

    datasets = ['train', 'valid', 'test']

    #corpora = [os.path.join(args.raw_path, f) for f in os.listdir(args.raw_path)
    #           if not f.startswith('.') and f.endswith('.json')]
    print("")
    for corpus_type in datasets:
        a_lst = []
        for json_f in glob.glob(pjoin(args.raw_path, '*' + corpus_type + '.*.json')):
            real_name = json_f.split('/')[-1]
            #print("json_f:", json_f, real_name)
            a_lst.append((corpus_type, json_f, args, pjoin(args.save_path, real_name.replace('json', 'bert.pt'))))
        
        pool = Pool(args.n_cpus)
        for d in pool.imap(_format_to_bert, a_lst):
            pass
        pool.close()
        pool.join()


def format_to_pico_adapter(args):
    print('... (5) Converting data to pico adapter data... this will take a while')

    datasets = ['train', 'valid', 'test']

    # corpora = [os.path.join(args.raw_path, f) for f in os.listdir(args.raw_path)
    #           if not f.startswith('.') and f.endswith('.json')]
    print("")
    for corpus_type in datasets:
        a_lst = []
        for json_f in glob.glob(pjoin(args.raw_path, '*' + corpus_type + '.*.json')):
            real_name = json_f.split('/')[-1]
            # print("json_f:", json_f, real_name)
            a_lst.append((corpus_type, json_f, args, pjoin(args.save_path, real_name.replace('json', 'padpter.pt'))))

        pool = Pool(args.n_cpus)
        for d in pool.imap(_format_to_pico_adapter, a_lst):
            pass
        pool.close()
        pool.join()

def _format_to_pico_adapter(params):
    corpus_type, json_file, args, save_file = params
    is_test = corpus_type == 'test'
    if (os.path.exists(save_file)):
        logger.info('Ignore %s' % save_file)
        return

    pico_adapter = PicoAdapterData(args)

    logger.info('Processing %s' % json_file)
    jobs = json.load(open(json_file))
    datasets = []
    for d in jobs:
        source, tag = d['src'], d['tag']
        #sent_labels = greedy_selection(source[:args.max_src_nsents], tgt, 3)
        if (args.lower):
            source = [' '.join(s).lower().split() for s in source]
            #tgt = [' '.join(s).lower().split() for s in tgt]
        b_data = pico_adapter.preprocess(source, tag, is_test=is_test)
        # b_data = bert.preprocess(source, tgt, sent_labels, use_bert_basic_tokenizer=args.use_bert_basic_tokenizer)

        if (b_data is None):
            continue
        src_subtoken_idxs, src_tag_idx, mask_label  = b_data
        b_data_dict = {"src": src_subtoken_idxs, "tag": src_tag_idx, "mask": mask_label}
        datasets.append(b_data_dict)
    logger.info('Processed instances %d' % len(datasets))
    logger.info('Saving to %s' % save_file)
    torch.save(datasets, save_file)
    datasets = []
    gc.collect()

def _format_to_bert(params):
    corpus_type, json_file, args, save_file = params
    is_test = corpus_type == 'test'
    if (os.path.exists(save_file)):
        logger.info('Ignore %s' % save_file)
        return

    bert = BertData(args)

    logger.info('Processing %s' % json_file)
    jobs = json.load(open(json_file))
    datasets = []
    for d in jobs:
        source, tgt = d['src'], d['tgt']
        sent_labels = greedy_selection(source[:args.max_src_nsents], tgt, 3)
        if (args.lower):
            source = [' '.join(s).lower().split() for s in source]
            tgt = [' '.join(s).lower().split() for s in tgt]
        b_data = bert.preprocess(source, tgt, sent_labels, use_bert_basic_tokenizer=args.use_bert_basic_tokenizer,
                                 is_test=is_test)
        # b_data = bert.preprocess(source, tgt, sent_labels, use_bert_basic_tokenizer=args.use_bert_basic_tokenizer)

        if (b_data is None):
            continue
        src_subtoken_idxs, sent_labels, tgt_subtoken_idxs, segments_ids, cls_ids, src_txt, tgt_txt = b_data
        b_data_dict = {"src": src_subtoken_idxs, "tgt": tgt_subtoken_idxs,
                       "src_sent_labels": sent_labels, "segs": segments_ids, 'clss': cls_ids,
                       'src_txt': src_txt, "tgt_txt": tgt_txt}
        datasets.append(b_data_dict)
    logger.info('Processed instances %d' % len(datasets))
    logger.info('Saving to %s' % save_file)
    torch.save(datasets, save_file)
    datasets = []
    gc.collect()


def format_to_lines(args):
    corpora = sorted([os.path.join(args.raw_path, f) for f in os.listdir(args.raw_path)
                      if not f.startswith('.') and not f.endswith('.abs.txt.json') and not f.endswith('.tag.json')])
    #train_files, valid_files, test_files = [], [], []:

    args_list = []
    for f_main in corpora:
        f_abs_name = '{}.abs.txt.json'.format(os.path.basename(f_main).split('.')[0])
        f_abs = os.path.join(args.raw_path, f_abs_name)
        f_tag_name = '{}.tag.json'.format(os.path.basename(f_main).split('.')[0])
        f_tag = os.path.join(args.raw_path, f_tag_name)
        args_list.append((f_main, f_abs, f_tag, args))
    index_list = list(range(len(args_list)))
    random.shuffle(index_list) 
    train_list_id = index_list[:int(len(args_list)*0.75)] 
    eval_list_id = index_list[int(len(args_list)*0.75)+1:int(len(args_list)*0.9)]
    test_list_id = index_list[int(len(args_list)*0.9)+1:]
    train_files = [args_list[i] for i in train_list_id]
    valid_files = [args_list[i] for i in eval_list_id]
    test_files = [args_list[i] for i in test_list_id]

    start = time.time()
    print('... (4) Packing tokenized data into shards...')
    print('Converting files count: {}'.format(len(corpora)))

    # imap executes in sync multiprocess manner
    # use array and shard_size to save the flow of ordered data
    corporas = {'train': train_files, 'valid': valid_files, 'test': test_files}
    for corpus_type in ['train', 'valid', 'test']:
        a_lst = corporas[corpus_type]
        pool = Pool(args.n_cpus)
        dataset = []
        shard_count = 0
        with tqdm(total=len(a_lst)) as pbar:
            with tqdm(total=args.shard_size) as spbar:
                for i, data in enumerate(pool.imap(_format_to_lines, a_lst)):
                    if data:
                        dataset.append(data)
                    spbar.update()
                    if (len(dataset) > args.shard_size):
                        fpath = "{:s}/{:s}.{:d}.json".format(args.save_path, corpus_type, shard_count)
                        with open(fpath, 'w') as f:
                            f.write(json.dumps(dataset))
                        dataset = []
                        shard_count += 1
                        pbar.update()
                        spbar.reset()
                        # gc.collect()
                spbar.close()
            pbar.close()
        pool.close()
        pool.join()
        if len(dataset) > 0:
            fpath = "{:s}/{:s}.{:d}.json".format(args.save_path, corpus_type, shard_count)
            print('last shard {} saved'.format(shard_count))
            with open(fpath, 'w') as f:
                f.write(json.dumps(dataset))
            dataset = []
            shard_count += 1
    end = time.time()
    print('... Ending (4), time elapsed {}'.format(end - start))

def _format_to_lines(params):
    f_main, f_abs, f_tags, args = params
    source, tgt, tag = load_json(f_main, f_abs, f_tags)
    if not source:
        return None
    else:
        return {'src': source, 'tgt': tgt, "tag":tag}

def format_xsum_to_lines(args):
    if (args.dataset != ''):
        datasets = [args.dataset]
    else:
        datasets = ['train', 'test', 'valid']

    corpus_mapping = json.load(open(pjoin(args.raw_path, 'XSum-TRAINING-DEV-TEST-SPLIT-90-5-5.json')))

    for corpus_type in datasets:
        mapped_fnames = corpus_mapping[corpus_type]
        root_src = pjoin(args.raw_path, 'restbody')
        root_tgt = pjoin(args.raw_path, 'firstsentence')
        # realnames = [fname.split('.')[0] for fname in os.listdir(root_src)]
        realnames = mapped_fnames

        a_lst = [(root_src, root_tgt, n) for n in realnames]
        pool = Pool(args.n_cpus)
        dataset = []
        p_ct = 0
        for d in pool.imap_unordered(_format_xsum_to_lines, a_lst):
            if (d is None):
                continue
            dataset.append(d)
            if (len(dataset) > args.shard_size):
                pt_file = "{:s}.{:s}.{:d}.json".format(args.save_path, corpus_type, p_ct)
                with open(pt_file, 'w') as save:
                    save.write(json.dumps(dataset))
                    p_ct += 1
                    dataset = []

        pool.close()
        pool.join()
        if (len(dataset) > 0):
            pt_file = "{:s}.{:s}.{:d}.json".format(args.save_path, corpus_type, p_ct)
            with open(pt_file, 'w') as save:
                save.write(json.dumps(dataset))
                p_ct += 1
                dataset = []


def _format_xsum_to_lines(params):
    src_path, root_tgt, name = params
    f_src = pjoin(src_path, name + '.restbody')
    f_tgt = pjoin(root_tgt, name + '.fs')
    if (os.path.exists(f_src) and os.path.exists(f_tgt)):
        print(name)
        source = []
        for sent in open(f_src):
            source.append(sent.split())
        tgt = []
        for sent in open(f_tgt):
            tgt.append(sent.split())
        return {'src': source, 'tgt': tgt}
    return None

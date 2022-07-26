import copy

import torch
import torch.nn as nn
from transformers import BertModel, BertConfig, RobertaConfig, RobertaModel, AutoTokenizer, AutoModel
from torch.nn.init import xavier_uniform_

from models.decoder import TransformerDecoder
from models.encoder import Classifier, ExtTransformerEncoder
from models.optimizers import Optimizer
from transformers.adapters.composition import Fuse


def build_optim(args, model, checkpoint):
    """ Build optimizer """

    if checkpoint is not None:
        optim = checkpoint['optim']
        saved_optimizer_state_dict = optim.optimizer.state_dict()
        optim.optimizer.load_state_dict(saved_optimizer_state_dict)
        if args.visible_gpus != '-1':
            for state in optim.optimizer.state.values():
                for k, v in state.items():
                    if torch.is_tensor(v):
                        state[k] = v.cuda()

        if (optim.method == 'adam') and (len(optim.optimizer.state) < 1):
            raise RuntimeError(
                "Error: loaded Adam optimizer from existing model" +
                " but optimizer state is empty")

    else:
        optim = Optimizer(
            args.optim, args.lr, args.max_grad_norm,
            beta1=args.beta1, beta2=args.beta2,
            decay_method='noam',
            warmup_steps=args.warmup_steps)

    optim.set_parameters(list(model.named_parameters()))

    return optim


def build_optim_bert(args, model, checkpoint):
    """ Build optimizer """

    if checkpoint is not None:
        optim = checkpoint['optims'][0]
        saved_optimizer_state_dict = optim.optimizer.state_dict()
        optim.optimizer.load_state_dict(saved_optimizer_state_dict)
        if args.visible_gpus != '-1':
            for state in optim.optimizer.state.values():
                for k, v in state.items():
                    if torch.is_tensor(v):
                        state[k] = v.cuda()

        if (optim.method == 'adam') and (len(optim.optimizer.state) < 1):
            raise RuntimeError(
                "Error: loaded Adam optimizer from existing model" +
                " but optimizer state is empty")

    else:
        optim = Optimizer(
            args.optim, args.lr_bert, args.max_grad_norm,
            beta1=args.beta1, beta2=args.beta2,
            decay_method='noam',
            warmup_steps=args.warmup_steps_bert)

    params = [(n, p) for n, p in list(model.named_parameters()) if n.startswith('bert.model')]
    optim.set_parameters(params)

    return optim


def build_optim_dec(args, model, checkpoint):
    """ Build optimizer """

    if checkpoint is not None:
        optim = checkpoint['optims'][1]
        saved_optimizer_state_dict = optim.optimizer.state_dict()
        optim.optimizer.load_state_dict(saved_optimizer_state_dict)
        if args.visible_gpus != '-1':
            for state in optim.optimizer.state.values():
                for k, v in state.items():
                    if torch.is_tensor(v):
                        state[k] = v.cuda()

        if (optim.method == 'adam') and (len(optim.optimizer.state) < 1):
            raise RuntimeError(
                "Error: loaded Adam optimizer from existing model" +
                " but optimizer state is empty")

    else:
        optim = Optimizer(
            args.optim, args.lr_dec, args.max_grad_norm,
            beta1=args.beta1, beta2=args.beta2,
            decay_method='noam',
            warmup_steps=args.warmup_steps_dec)

    params = [(n, p) for n, p in list(model.named_parameters()) if not n.startswith('bert.model')]
    optim.set_parameters(params)

    return optim


def get_generator(vocab_size, dec_hidden_size, device):
    gen_func = nn.LogSoftmax(dim=-1)
    generator = nn.Sequential(
        nn.Linear(dec_hidden_size, vocab_size),
        gen_func
    )
    generator.to(device)

    return generator


class RoBerta(nn.Module):
    def __init__(self, large, temp_dir, finetune, model, device,args):
        super(RoBerta, self).__init__()
        if (large):
            if model == "robert":
                self.model = RobertaModel.from_pretrained('roberta-large', cache_dir=temp_dir)
            if model == "bert":
                self.model = BertModel.from_pretrained('bert-large-uncased', cache_dir=temp_dir)
            if model == "pubmed":
                model_name = 'microsoft/BiomedNLP-PubMedBERT-base-uncased-abstract'
                self.model = AutoModelForMaskedLM.from_pretrained(model_name).to(device)
            if model == "biobert":
                model_name = 'dmis-lab/biobert-v1.1'
                self.model = AutoModelForMaskedLM.from_pretrained(model_name).to(device)
            self.model.add_adapter("finetune")

            self.model.load_adapter("./final_adapter", load_as="ner", with_head=False)
            self.model.add_fusion(Fuse("finetune", "ner"))
            self.model.set_active_adapters(Fuse("finetune", "ner"))
            adapter_setup = Fuse("finetune", "ner")
            self.model.train_fusion(adapter_setup)
            self.model.encoder.enable_adapters(adapter_setup, True, True)
        else:
            if model == "robert":
                self.model = RobertaModel.from_pretrained('roberta-base', cache_dir=temp_dir)
                if args.adapter_training_strategy != 'basic':
                    if args.adapter_training_strategy == 'both':
                        self.model.load_adapter(args.adapter_path_robert_generative, load_as="mlm",with_head=False)
                        self.model.load_adapter(args.adapter_path_robert_discriminative, load_as="ner",with_head=False)
                    if args.adapter_training_strategy == 'discriminative':
                        self.model.load_adapter(args.adapter_path_robert_discriminative, load_as="ner",with_head=False)
                    if args.adapter_training_strategy == 'generative':
                        self.model.load_adapter(args.adapter_path_robert_generative, load_as="mlm",with_head=False)
            if model == "bert":
                self.model = BertModel.from_pretrained('bert-base-uncased', cache_dir=temp_dir)
                if args.adapter_training_strategy != 'basic':
                    if args.adapter_training_strategy == 'both':
                        self.model.load_adapter(args.adapter_path_bert_generative, load_as="mlm",with_head=False)
                        self.model.load_adapter(args.adapter_path_bert_discriminative, load_as="ner",with_head=False)
                    if args.adapter_training_strategy == 'discriminative':
                        self.model.load_adapter(args.adapter_path_bert_discriminative, load_as="ner",with_head=False)
                    if args.adapter_training_strategy == 'generative':
                        self.model.load_adapter(args.adapter_path_bert_generative, load_as="mlm",with_head=False)
            if model == "pubmed":
                model_name = 'microsoft/BiomedNLP-PubMedBERT-base-uncased-abstract'
                self.model = AutoModel.from_pretrained(model_name).to(device)
                if args.adapter_training_strategy != 'basic':
                    if args.adapter_training_strategy == 'both':
                        self.model.load_adapter(args.adapter_path_pubmed_generative, load_as="mlm",with_head=False)
                        self.model.load_adapter(args.adapter_path_pubmed_discriminative, load_as="ner",with_head=False)
                    if args.adapter_training_strategy == 'discriminative':
                        self.model.load_adapter(args.adapter_path_pubmed_discriminative, load_as="ner",with_head=False)
                    if args.adapter_training_strategy == 'generative':
                        self.model.load_adapter(args.adapter_path_pubmed_generative, load_as="mlm",with_head=False)
            if model == "biobert":
                model_name = 'dmis-lab/biobert-v1.1'
                self.model = AutoModel.from_pretrained(model_name).to(device)
                if args.adapter_training_strategy != 'basic':
                    if args.adapter_training_strategy == 'both':
                        self.model.load_adapter(args.adapter_path_pubmed_generative, load_as="mlm",with_head=False)
                        self.model.load_adapter(args.adapter_path_pubmed_discriminative, load_as="ner",with_head=False)
                    if args.adapter_training_strategy == 'discriminative':
                        self.model.load_adapter(args.adapter_path_pubmed_discriminative, load_as="ner",with_head=False)
                    if args.adapter_training_strategy == 'generative':
                        self.model.load_adapter(args.adapter_path_pubmed_generative, load_as="mlm",with_head=False)
            if args.adapter_training_strategy != 'basic':
                self.model.add_adapter("finetune")
                if args.adapter_training_strategy == 'both':
                    self.model.add_fusion(Fuse("mlm", "ner", "finetune"))
                    self.model.set_active_adapters(Fuse("mlm", 'ner', "finetune"))
                    adapter_setup = Fuse("mlm", 'ner', "finetune")
                elif args.adapter_training_strategy == 'discriminative':
                    self.model.add_fusion(Fuse("finetune", "ner"))
                    self.model.set_active_adapters(Fuse("finetune", "ner"))
                    adapter_setup = Fuse("finetune", "ner")
                elif args.adapter_training_strategy == 'generative':
                    self.model.add_fusion(Fuse("mlm", "finetune"))
                    self.model.set_active_adapters(Fuse("mlm","finetune"))
                    adapter_setup = Fuse("mlm","finetune")
                self.model.train_fusion(adapter_setup)
                self.model.encoder.enable_adapters(adapter_setup, True, True)
                #self.model.freeze_model(freeze=False)
            else:
                self.model.add_adapter("finetune")
                self.model.train_adapter("finetune")
                self.model.set_active_adapters("finetune")
        self.finetune = finetune

    def forward(self, x, segs, mask):
        if (self.finetune):
            #if args.model=='bert':
            #    top_vec, _ = self.model(x, segs, attention_mask=mask)
            #else:
            output = self.model(input_ids=x, token_type_ids=segs, attention_mask=mask)
            top_vec = output.last_hidden_state
        else:
            self.eval()
            with torch.no_grad():
                #if args.model=="bert":
                #    top_vec, _ = self.model(x, segs, attention_mask=mask)
                #else:
                output = self.model(input_ids=x, token_type_ids=segs, attention_mask=mask)
                top_vec = output.last_hidden_state
        return top_vec


class ExtSummarizer(nn.Module):
    def __init__(self, args, device, checkpoint):
        super(ExtSummarizer, self).__init__()
        self.args = args
        self.device = device
        self.RoBerta = RoBerta(args.large, args.temp_dir, args.finetune_bert, args.model, device, args)
        self.ext_layer = ExtTransformerEncoder(self.RoBerta.model.config.hidden_size, args.ext_ff_size, args.ext_heads,
                                               args.ext_dropout, args.ext_layers)
        if (args.encoder == 'baseline'):
            roberta_config = RobertaConfig(self.RoBerta.model.config.vocab_size, hidden_size=args.ext_hidden_size,
                                           num_hidden_layers=args.ext_layers, num_attention_heads=args.ext_heads,
                                           intermediate_size=args.ext_ff_size)
            self.RoBerta.model = RobertaModel(roberta_config)
            self.ext_layer = Classifier(self.RoBerta.model.config.hidden_size)

        if (args.max_pos > 512):
            my_pos_embeddings = nn.Embedding(args.max_pos, self.RoBerta.model.config.hidden_size)
            my_pos_embeddings.weight.data[:512] = self.RoBerta.model.embeddings.position_embeddings.weight.data
            my_pos_embeddings.weight.data[512:] = self.RoBerta.model.embeddings.position_embeddings.weight.data[-1][
                                                  None, :].repeat(args.max_pos - 512, 1)
            self.RoBerta.model.embeddings.position_embeddings = my_pos_embeddings

        if checkpoint is not None:
            self.load_state_dict(checkpoint['model'], strict=True)
        else:
            if args.param_init != 0.0:
                for p in self.ext_layer.parameters():
                    p.data.uniform_(-args.param_init, args.param_init)
            if args.param_init_glorot:
                for p in self.ext_layer.parameters():
                    if p.dim() > 1:
                        xavier_uniform_(p)

        self.to(device)

    def forward(self, src, segs, clss, mask_src, mask_cls):
        top_vec = self.RoBerta(src, segs, mask_src)
        sents_vec = top_vec[torch.arange(top_vec.size(0)).unsqueeze(1), clss]
        sents_vec = sents_vec * mask_cls[:, :, None].float()
        sent_scores = self.ext_layer(sents_vec, mask_cls).squeeze(-1)
        return sent_scores, mask_cls


class AbsSummarizer(nn.Module):
    def __init__(self, args, device, checkpoint=None, bert_from_extractive=None):
        super(AbsSummarizer, self).__init__()
        self.args = args
        self.device = device
        self.bert = Bert(args.large, args.temp_dir, args.finetune_bert)

        if bert_from_extractive is not None:
            self.bert.model.load_state_dict(
                dict([(n[11:], p) for n, p in bert_from_extractive.items() if n.startswith('bert.model')]), strict=True)

        if (args.encoder == 'baseline'):
            bert_config = BertConfig(self.bert.model.config.vocab_size, hidden_size=args.enc_hidden_size,
                                     num_hidden_layers=args.enc_layers, num_attention_heads=8,
                                     intermediate_size=args.enc_ff_size,
                                     hidden_dropout_prob=args.enc_dropout,
                                     attention_probs_dropout_prob=args.enc_dropout)
            self.bert.model = BertModel(bert_config)

        if (args.max_pos > 512):
            my_pos_embeddings = nn.Embedding(args.max_pos, self.bert.model.config.hidden_size)
            my_pos_embeddings.weight.data[:512] = self.bert.model.embeddings.position_embeddings.weight.data
            my_pos_embeddings.weight.data[512:] = self.bert.model.embeddings.position_embeddings.weight.data[-1][None,
                                                  :].repeat(args.max_pos - 512, 1)
            self.bert.model.embeddings.position_embeddings = my_pos_embeddings
        self.vocab_size = self.bert.model.config.vocab_size
        tgt_embeddings = nn.Embedding(self.vocab_size, self.bert.model.config.hidden_size, padding_idx=0)
        if (self.args.share_emb):
            tgt_embeddings.weight = copy.deepcopy(self.bert.model.embeddings.word_embeddings.weight)

        self.decoder = TransformerDecoder(
            self.args.dec_layers,
            self.args.dec_hidden_size, heads=self.args.dec_heads,
            d_ff=self.args.dec_ff_size, dropout=self.args.dec_dropout, embeddings=tgt_embeddings)

        self.generator = get_generator(self.vocab_size, self.args.dec_hidden_size, device)
        self.generator[0].weight = self.decoder.embeddings.weight

        if checkpoint is not None:
            self.load_state_dict(checkpoint['model'], strict=True)
        else:
            for module in self.decoder.modules():
                if isinstance(module, (nn.Linear, nn.Embedding)):
                    module.weight.data.normal_(mean=0.0, std=0.02)
                elif isinstance(module, nn.LayerNorm):
                    module.bias.data.zero_()
                    module.weight.data.fill_(1.0)
                if isinstance(module, nn.Linear) and module.bias is not None:
                    module.bias.data.zero_()
            for p in self.generator.parameters():
                if p.dim() > 1:
                    xavier_uniform_(p)
                else:
                    p.data.zero_()
            if (args.use_bert_emb):
                tgt_embeddings = nn.Embedding(self.vocab_size, self.bert.model.config.hidden_size, padding_idx=0)
                tgt_embeddings.weight = copy.deepcopy(self.bert.model.embeddings.word_embeddings.weight)
                self.decoder.embeddings = tgt_embeddings
                self.generator[0].weight = self.decoder.embeddings.weight

        self.to(device)

    def forward(self, src, tgt, segs, clss, mask_src, mask_tgt, mask_cls):
        top_vec = self.bert(src, segs, mask_src)
        dec_state = self.decoder.init_decoder_state(src, top_vec)
        decoder_outputs, state = self.decoder(tgt[:, :-1], top_vec, dec_state)
        return decoder_outputs, None

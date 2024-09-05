import sys
from models.diffusion import StableDiffusion
from utils.model_converter import load_weights_from_ckpt
from transformers import CLIPTokenizer
import os
import time

def create_model(model_path):
    model = StableDiffusion(model_type='txt2img')
    loaded_state_dict = load_weights_from_ckpt(model_path, device='cpu')
    model.vae.load_state_dict(loaded_state_dict['vae'], strict=True)
    model.unet.load_state_dict(loaded_state_dict['unet'], strict=True)
    model.cond_encoder.load_state_dict(loaded_state_dict['cond_encoder'], strict=True)
    return model

def create_tokenizer(tokenizer_dir): 
    tokenizer = CLIPTokenizer(os.path.join(tokenizer_dir, 'tokenizer_vocab.json'), merges_file=os.path.join(tokenizer_dir, 'tokenizer_merges.txt'))
    return tokenizer

def load_model(args):
    start_time = time.time()
    model = create_model(args.model_path)
    print(f"Loaded model in {time.time() - start_time:.2f}s")
    start_time = time.time()
    tokenizer = create_tokenizer(args.tokenizer_dir)
    print(f"Loaded tokenizer in {time.time() - start_time:.2f}s")
    return model, tokenizer
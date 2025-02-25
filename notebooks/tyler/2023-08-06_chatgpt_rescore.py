##
import numpy as np
import sys, os
import openai, jiwer
import tiktoken
from scipy.io import loadmat
# horrible hack to get around this repo not being a proper python package
SCRIPT_DIR = os.path.dirname(os.path.dirname(os.getcwd()))
sys.path.append(SCRIPT_DIR)
from data_utils import TextTransform
from pqdm.threads import pqdm

# can use tenacity or backoff
# https://platform.openai.com/docs/guides/rate-limits/retrying-with-exponential-backoff
from tenacity import (
    retry,
    stop_after_attempt,
    wait_random_exponential,
)  # for exponential backoff


# TODO: still hangs on last example or two. Maybe rebase to:
# https://github.com/openai/openai-cookbook/blob/c651bfdda64ac049747c2a174cde1c946e2baf1d/examples/api_request_parallel_processor.py
@retry(wait=wait_random_exponential(min=1, max=60), stop=stop_after_attempt(6))
def completions_with_backoff(**kwargs):
    return openai.ChatCompletion.create(**kwargs)


tokenizer = tiktoken.encoding_for_model("gpt-3.5-turbo")
text_transform = TextTransform(togglePhones = False)

#### using the 26.4% WER model
# npz = np.load("/scratch/users/tbenst/2023-08-01T06:54:28.359594_gaddy/SpeechOrEMGToText-epoch=199-val/top100_500beams.npz",
#               allow_pickle=True)

# best so far (top100_5000beams_thresh75.npz)
npz = np.load("/scratch/users/tbenst/2023-08-01T06:54:28.359594_gaddy/SpeechOrEMGToText-epoch=199-val/top100_5000beams.npz",
              allow_pickle=True)

# npz = np.load("/scratch/users/tbenst/2023-08-01T06:54:28.359594_gaddy/SpeechOrEMGToText-epoch=199-val/top100_5000beams_thresh150.npz",
#               allow_pickle=True)
# npz = np.load("/scratch/users/tbenst/2023-08-01T06:54:28.359594_gaddy/SpeechOrEMGToText-epoch=199-val/top100_150beams_thresh50.npz",
#               allow_pickle=True)
# npz = np.load("/scratch/users/tbenst/2023-08-01T06:54:28.359594_gaddy/SpeechOrEMGToText-epoch=199-val/top100_150beams_thresh50_lmweight1.85.npz",
#               allow_pickle=True)



#### lowest CTC loss model (27.5% WER -> 26.4% w/ better beam search)
# 24.1% WER after LLM rescoring
# npz = np.load("/scratch/users/tbenst/2023-08-05T02:28:07.543866_gaddy/SpeechOrEMGToText-epoch=193-val/top100_5000beams_thresh75.npz",
#               allow_pickle=True)


##
sys_msg = """
Your task is automatic speech recognition. \
Below are the candidate transcriptions along with their \
negative log-likelihood from a CTC beam search. \
Respond with the correct transcription, \
without any introductory text.
""".strip()

def create_rescore_msg(predictions, scores):
    rescore_msg = '\n'.join([f"{s:.3f}\t{p}"
        for p,s in zip(predictions, scores)])
    return rescore_msg

def predict_from_topk(predictions, scores, sys_msg=sys_msg):
    """Use OpenAI's chat API to predict from topk beam search results.
    
    GPT-3-turbo (4k or 8k)
    cost: $0.0015 or $0.003 / 1K tokens
    avg tokens per call: 1500
    cost per validation loop (200 examples): $0.45 - $0.9
    
    GPT-4 (8k)
    cost: $0.03 / 1K tokens
    cost per validation loop: $9
    """
    rescore_msg = create_rescore_msg(predictions, scores)
    num_tokens = num_tokens_from_string(rescore_msg) + num_tokens_from_string(sys_msg)
    if num_tokens > 4096:
        model = "gpt-3.5-turbo-16k"
    else:
        model = "gpt-3.5-turbo"
    # model="gpt-4"

    num_pred = len(predictions)
    if num_pred < 10:
        print(f"WARNING: only {num_pred} predictions from beam search")
    
    cc = completions_with_backoff(
        model=model,
        messages=[
                {"role": "system", "content": sys_msg},
                {"role": "user", "content": rescore_msg},
            ],
        temperature=0.0
    )
    
    return cc.choices[0].message.content

def num_tokens_from_string(string: str, tokenizer=tokenizer) -> int:
    """Returns the number of tokens in a text string."""
    num_tokens = len(tokenizer.encode(string))
    return num_tokens

def calc_wer(predictions, targets):
    """Calculate WER from predictions and targets.
    
    predictions: list of strings
    targets: list of strings
    """
    # print(targets, predictions)
    targets = list(map(text_transform.clean_2, targets))
    predictions = list(map(text_transform.clean_2, predictions))
    transformation = jiwer.Compose([jiwer.RemovePunctuation(), jiwer.ToLowerCase()])
    targets     = transformation(targets)
    predictions = transformation(predictions)
    # print(targets, predictions)
    return jiwer.wer(targets, predictions)

def batch_predict_from_topk(predictions, scores, sys_msg=sys_msg):
    pt = lambda x: predict_from_topk(*x, sys_msg=sys_msg)
    # can only request up to 90k tokens / minute
    # njobs = 16 # rate limited
    # njobs = 4 # 2:28
    njobs = 3 # 1:31, sometimes 10min tho..?
    # njobs = 2 # 3:06, also sometimes 10min
    transcripts = pqdm(zip(predictions, scores), pt, n_jobs=njobs)
    for i,t in enumerate(transcripts):
        if type(t) != str:
            print(i,t)
            p = predict_from_topk(npz['predictions'][i], npz['beam_scores'][i])
            transcripts[i] = p
    return transcripts
    # return [predict_from_topk(pred, score, sys_msg)
    #     for pred, score in tqdm(zip(predictions, scores))]
    
def clean_transcripts(transcripts):
    transcripts = list(map(text_transform.clean_2, transcripts))
    transformation = jiwer.Compose([jiwer.RemovePunctuation(), jiwer.ToLowerCase()])
    transcripts     = transformation(transcripts)
    for i in range(len(transcripts)):
        if transcripts[i].startswith("the correct transcription is "):
            transcripts[i] = transcripts[i].replace("the correct transcription is ", "")
            
    return transcripts
##
# 26.3% WER with 150 beams
# i think 26.0% with 500 beams
# baseline (25.5% for 5000 beams!)
calc_wer([n[0] for n in npz['predictions']], npz['sentences'])
##
transcripts = batch_predict_from_topk(npz['predictions'], npz['beam_scores'])
calc_wer(clean_transcripts(transcripts), npz['sentences'])

##
sys_msg2 = """
You are a rescoring algorithm for automatic speech recognition. \
Given the results of a beam search, with candidate hypotheses and their score, \
respond with the correct transcription.
""".strip()

# transcript = predict_from_topk(npz['predictions'][0], npz['beam_scores'][0])
# transcript = create_rescore_msg(npz['predictions'][0], npz['beam_scores'][0])

transcripts = batch_predict_from_topk(npz['predictions'], npz['beam_scores'], sys_msg=sys_msg2)
# 24.7% on 500 beams
# 24.3% on 5000 beams
calc_wer(clean_transcripts(transcripts), npz['sentences'])

##
sys_msg2 = "You are a rescoring algorithm for automatic speech recognition, focusing on generating coherent and contextually relevant transcriptions. Given a list of candidate transcriptions with scores produced by a beam search, your task is to deduce the most likely transcription that makes sense contextually and grammatically, even if it's not explicitly present in the given options."
transcripts = batch_predict_from_topk(npz['predictions'], npz['beam_scores'], sys_msg=sys_msg2)
calc_wer(clean_transcripts(transcripts), npz['sentences']) # > 100%
##
sys_msg2 = "You are a rescoring algorithm for automatic speech recognition, focusing on generating coherent and contextually relevant transcriptions. Given a list of candidate transcriptions with scores produced by a beam search, your task is to deduce the most likely transcription that makes sense contextually and grammatically from the given options. Your response should be the corrected transcription only, without any additional explanation or introductory text. Make only minimal changes to correct grammar and coherence, and ensure that the transcription is closely based on one of the provided candidates."
transcripts = batch_predict_from_topk(npz['predictions'], npz['beam_scores'], sys_msg=sys_msg2)
calc_wer(clean_transcripts(transcripts), npz['sentences']) # 26.3%
##
sys_msg2 = "You are a rescoring algorithm for automatic speech recognition, focusing on generating coherent and contextually relevant transcriptions. Given a list of candidate transcriptions with scores produced by a beam search, your task is to deduce the most likely transcription that makes sense contextually and grammatically, even if it's not explicitly present in the given options. Your response should be the corrected transcription only, without any additional explanation or introductory text."
transcripts = batch_predict_from_topk(npz['predictions'], npz['beam_scores'], sys_msg=sys_msg2)
calc_wer(clean_transcripts(transcripts), npz['sentences']) # 24.49%
##
sys_msg2 = """
Your task is automatic speech recognition. \
Below are the candidate transcriptions along with their \
negative log-likelihood from a CTC beam search. \
Respond with the correct transcription, \
without any introductory text.
""".strip()
transcripts = batch_predict_from_topk(npz['predictions'], npz['beam_scores'], sys_msg=sys_msg2)
calc_wer(clean_transcripts(transcripts), npz['sentences']) # 22.85% !!!!
# 23.54% on the 150 threshold version
# 23.06% on retry - some stochasticy in results...
##
sys_msg2 = """
Your task is automatic speech recognition. \
Below are the candidate transcriptions along with their \
negative log-likelihood from a Connectionist Temporal Classification (CTC) \
prefix beam search with a 3-gram language model constraint.  \
Respond with the correct transcription, \
without any introductory text.
""".strip()
transcripts = batch_predict_from_topk(npz['predictions'], npz['beam_scores'], sys_msg=sys_msg2)
calc_wer(clean_transcripts(transcripts), npz['sentences']) # 23.86%
##
sys_msg2 = """
Your task is speech recognition. Given the candidate transcriptions and their \
negative log-likelihood from a CTC beam search, provide the correct transcription.
""".strip()
transcripts = batch_predict_from_topk(npz['predictions'], npz['beam_scores'], sys_msg=sys_msg2)
calc_wer(clean_transcripts(transcripts), npz['sentences']) # 25.5%
##
sys_msg2 = """
Perform automatic speech recognition. Given the candidate transcriptions and their negative log-likelihood from a CTC beam search, provide the correct transcription.
""".strip()
transcripts = batch_predict_from_topk(npz['predictions'], npz['beam_scores'], sys_msg=sys_msg2)
calc_wer(clean_transcripts(transcripts), npz['sentences']) # 23.88%
##
sys_msg2 = """
You are performing automatic speech recognition. Use the candidate transcriptions and their negative log-likelihood from a CTC beam search to determine the correct transcription.
""".strip()
transcripts = batch_predict_from_topk(npz['predictions'], npz['beam_scores'], sys_msg=sys_msg2)
calc_wer(clean_transcripts(transcripts), npz['sentences']) # 102%
##
sys_msg2 = """
Task: Automatic speech recognition. Candidates and their negative log-likelihood from a CTC beam search are provided. Respond with the correct transcription, without any introductory text.
""".strip()
transcripts = batch_predict_from_topk(npz['predictions'], npz['beam_scores'], sys_msg=sys_msg2)
calc_wer(clean_transcripts(transcripts), npz['sentences']) # 24.9%
##

for t, p, s in zip(clean_transcripts(transcripts), [p[0] for p in npz['predictions']], npz['sentences']):
    if t != p:
        print("===============")
        print(s)
        print(p)
        print(t)
##

import os
import csv
import json
import sqlite3
from pathlib import Path
from functools import partial
from concurrent import futures

import joblib
import numpy as np
from sklearn.preprocessing import Normalizer
from sklearn.model_selection import train_test_split
import matplotlib.pyplot as plt
from joblib import Parallel, delayed
from sklearn.metrics import plot_roc_curve

try:
    from . import draw
    from . import feature as fea
except ImportError:
    import draw
    import feature as fea


BASE_PATH = os.path.dirname(__file__)
RAA_DB = os.path.join(BASE_PATH, 'nr_raa_data.db')
NAA = ['A', 'C', 'D', 'E', 'F', 'G', 'H', 'I', 'K', 
       'L', 'M', 'N', 'P', 'Q', 'R', 'S', 'T', 'V', 'W', 'Y']


def check_aa(aa):
    if aa[0] == "-" or aa[-1] == "-":
        raise ValueError("amino acid cluster is wrong!")
    if "-" not in aa:
        raise ValueError("need an amino acid cluster!")
    aa = aa.strip().upper()
    cls_ls = list(aa.replace("-", "")).sort()
    if NAA.sort() != cls_ls:
        raise ValueError("amino acid cluster is wrong!")
    
# TODO - query optimization
def reduce_query(type_id, size):
    conn = sqlite3.connect(RAA_DB)
    cursor = conn.cursor()
    cursor.execute('select r.type_id, c.size, c.scheme, r.method from raa r \
                inner join cluster c on r.type_id=c.type_id \
                where  c.size in (%s) and r.type_id in (%s)' % (size, type_id))
    raa_clusters = cursor.fetchall()
    cursor.close()
    conn.commit()
    conn.close()
    return raa_clusters

def read_fasta(seq):
    seq_ls = []
    for line in seq:
        line = line.strip()
        if not line:
            continue
        if line[0] == '>':
            if seq_ls:
                yield descr, ''.join(seq_ls)
                seq_ls.clear()
            descr = line
        else:
            seq_ls.append(line)
    else:
        yield descr, ''.join(seq_ls)


def reduce(seqs, aa, raa=None):
    """ reduce seq based on rr
    :param seqs: seq lines, iter
    :param aa: cluster aa, list or tuple
    :param raa: representative aa, list or tuple
    :return:
    """
    if not raa:
        raa = [i.strip()[0] for i in aa]
    for i, j in zip(aa, raa):
        if j not in i:
            raise ValueError(f'raa or clustered_aa is wrong!')
    aa_dic = dict(zip(raa, aa))
    for seq in seqs:
        descr, seq = seq
        for key, val in aa_dic.items():
            if key == val:
                continue
            else:
                for ele in val:
                    seq = seq.replace(ele, key)
        yield descr, seq

            
def reduce_to_file(file, aa, output,):
    with output.open("w") as wh:
        rh = open(file, "r")
        h = csv.writer(wh)
        seqs = read_fasta(rh)
        reduced_seqs = reduce(seqs, aa)
        for descr, r_seq in reduced_seqs:
            wh.write(descr)
            wh.write("\n")
            for i in range(len(r_seq) // 80 + 1):
                wh.write(r_seq[i*80:(i+1)*80])
                wh.write("\n")
            else:
                wh.write(r_seq[(i+1)*80:])
        rh.close()

def batch_reduce(file, cluster_info, out_dir):
    with futures.ThreadPoolExecutor(len(cluster_info)) as tpe:
        to_do_map = {}
        for idx, item in enumerate(cluster_info, 1):
            type_id, size, cluster, _ = item
            aa = [i[0] for i in cluster.split("-")]
            type_n = "".join(["type",str(type_id)])
            rfile = out_dir / type_n / "-".join([str(size)]+["".join(aa)]) 
            mkdirs(rfile.parent)
            aa = cluster.split('-')
            future = tpe.submit(reduce_to_file, file, aa, rfile)
            to_do_map[future] = type_id, size, cluster
        done_iter = futures.as_completed(to_do_map)
        for i in done_iter:
            type_id, size, cluster = to_do_map[i]
            print("done %s %s %s" % (type_id, size, cluster)) 

def extract_to_file(feature_file, output, k, gap, lam, raa, label=0, mode="w"):
    with open(feature_file, "r") as rh, open(output, mode) as wh:
        h = csv.writer(wh)
        seqs = read_fasta(rh)
        for seq in seqs:
            _, seq = seq
            aa_vec = fea.seq_aac(seq, raa, k=k, gap=gap, lam=lam)
            if label is None:
                line = aa_vec
            else:
                line = [label] + aa_vec
            h.writerow(line)
            
# TODO - IO optimization     
def batch_extract(in_dir, out_dir, k, gap, lam, label=0, mode="w", n_jobs=1):
    def params(size_dir, type_dir):
        output = type_dir / (size_dir.stem + ".csv")
        raa_str = size_dir.stem.split("-")[-1]
        print(size_dir)
        return size_dir, output, list(raa_str)

    extract_fun = partial(extract_to_file, k=k, gap=gap, 
                          lam=lam, label=label, mode=mode, n_jobs=1)
    with Parallel(n_jobs=n_jobs) as pl:
        for types in in_dir.iterdir():
            type_dir = out_dir / types.name
            type_dir.mkdir(exist_ok=True)
            pl(delayed(extract_fun)(*params(size_dir, type_dir)) for size_dir in types.iterdir())

def roc_eval(x, y, model, out):
    svc_disp = plot_roc_curve(model, x, y)
    plt.savefig(out, dpi=1000, bbox_inches="tight")

def dic2array(result_dic, key='acc', cls=0):
    acc_ls = []  # all type acc
    type_ls = [type_id for type_id in result_dic.keys() if type_id != "naa"]
    type_ls.sort(key=lambda x: int(x[4:]))
    all_score_array = np.zeros([len(type_ls), 19])
    for idx, ti in enumerate(type_ls):
        type_ = result_dic[ti]
        score_size_ls = []
        for size in range(2, 21):
            score = type_.get(str(size), {key: [0]})[key][cls]
            score_size_ls.append(score)
        all_score_array[idx] = score_size_ls
    return all_score_array, type_ls

def filter_type(score_metric, type_ls, filter_num=8):
    filter_scores, filter_type = [], []
    for type_score, type_id in zip(score_metric, type_ls):
        scores = [i for i in type_score if i > 0]
        if len(scores) >= 8:
            filter_scores.append(type_score)
            filter_type.append(type_id)
    return np.array(filter_scores), filter_type
   
        
def eval_plot(result_dic, out, fmt, filter_num=8):
    key = 'acc'
    scores, types = dic2array(result_dic, key=key)
    f_scores, f_types = filter_type(scores, types, filter_num=filter_num)
    
    annot_size, tick_size, label_size = heatmap_font_size(scores.shape[1])
    f_annot_size, f_tick_size, f_label_size = heatmap_font_size(f_scores.shape[1])
    font_size = {"annot_size": annot_size, "tick_size": tick_size, "label_size": label_size}
    f_font_size = {"annot_size": f_annot_size, "tick_size": f_tick_size, "label_size": f_label_size}

    heatmap_path = out / f'{key}_heatmap.{fmt}'
    draw.p_acc_heat(scores.T, 0.6, 1, types, heatmap_path, **font_size)
    f_heatmap_path = out / f'f{filter_num}_{key}_heatmap.{fmt}'
    draw.p_acc_heat(f_scores.T, 0.6, 1, f_types, f_heatmap_path, **f_font_size)
    
    f_scores_arr = f_scores[f_scores > 0]
    size_arr = np.array([np.arange(2, 21)] * f_scores.shape[0])[f_scores > 0]
    acc_size_path = out / f'acc_size_density.{fmt}'
    acc_path = out / f'acc_density.{fmt}'
    size_arr = size_arr.flatten()
    draw.p_bivariate_density(size_arr, f_scores_arr, acc_size_path)
    draw.p_univariate_density(f_scores_arr*100, acc_path)

    max_type_idx_arr, max_size_idx_arr = np.where(f_scores == f_scores.max())
    m_type_idx, m_size_idx = max_type_idx_arr[0], max_size_idx_arr[0]  # 默认第一个
    cp_path = out / f'acc_comparsion.{fmt}'
    diff_size = f_scores[m_type_idx]
    same_size = f_scores[:, m_size_idx]
    types_label = [int(i[4:]) for i in f_types]
    draw.p_comparison_type(diff_size, same_size, types_label, cp_path)
    return f_scores_arr

def parse_path(feature_folder, filter_format='csv'):
    """
    :param feature_folder: all type feature folder path
    :return:
    """
    path = os.walk(feature_folder)
    for root, dirs, file in path:
        if root == feature_folder:
            continue
        yield root, [i for i in file if i.endswith(filter_format)]

def mkdirs(directory):
    try:
        os.makedirs(directory)
    except FileExistsError:
        pass

def load_normal_data(file_data, label_exist=True): ## file for data (x,y)
    if isinstance(file_data, (tuple, list)):
        if all([isinstance(i, np.ndarray) for i in file_data]):
            x, y = file_data
    if os.path.isfile(str(file_data)):
        data = np.genfromtxt(file_data, delimiter=',')
        if label_exist:
            x, y = data[:, 1:], data[:, 0]
        else:
            x, y = data, None
    scaler = Normalizer()
    x = scaler.fit_transform(x)
    return x, y

def load_model(model_path):
    model = joblib.load(model_path)
    return model

def feature_mix(files):
    data_ls = [np.genfromtxt(file, delimiter=',')[:, 1:] for file in files]
    mix_data = np.hstack(data_ls)
    y = np.genfromtxt(files[0], delimiter=',')[:, 0]
    x = mix_data
    return x, y

def merge_feature_file(label, file):
    if label is None:
        data_ls = [np.genfromtxt(file, delimiter=',') for file in file]
        mix_data = np.vstack(data_ls)
    else:
        data_ls = []
        for idx, f in zip(label, file):
            data = np.genfromtxt(f, delimiter=',')
            data[:, 0] = idx
            data_ls.append(data)
        mix_data = np.vstack(data_ls)
    return mix_data

def write_array(data, file):
    np.savetxt(file, data, delimiter=",", fmt="%.8f")
    
def split_data(file, test_size):
    data = np.genfromtxt(file, delimiter=',')
    data_train, data_test = train_test_split(data, test_size=test_size, random_state=1)
    return data_train, data_test
    
def param_grid(c, g):
    c_range = np.logspace(*c, base=2)
    gamma_range = np.logspace(*g, base=2)
    params = [{'kernel': ["rbf"], 'C': c_range,
                    'gamma': gamma_range}]
    return params

def save_y(out, *y):
    with open(out, "w") as f:
        fc = csv.writer(f)
        for line in zip(*y):
            fc.writerow(line)
    
def save_json(metric_dic, path):
    result_dic = {}
    naa_metric = {}
    for type_dir, metric_ls in metric_dic.items():
        result_dic.setdefault(type_dir.name, {})
        for size_dir, metrics in zip(type_dir.iterdir(), metric_ls):
            acc, sn, sp, ppv, mcc = metrics
            metric_dic = {'sn': sn.tolist(), 'sp': sp.tolist(), 'ppv': ppv.tolist(),
                      'acc': acc.tolist(), 'mcc': mcc.tolist()}
            if type_dir.name == "naa":
                naa_metric = metric_dic
            else:
                size_ = size_dir.name.split("-")[0]
                result_dic[type_dir.name][size_] = metric_dic
    for type_key in result_dic:
        result_dic[type_key]["20"] = naa_metric
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(result_dic, f, indent=4)


TEXT = """
    敏感度(Sensitivity, SN)也称召回率(Recall, RE):	
            Sn = Recall = TP / (TP + FN)
    特异性(Specificity, SP):
            Sp = TN / (TN + FP)
    精确率(Precision, PR)也称阳极预测值(Positive Predictive Value, PPV):	
            Precision= PPV = TP / (TP + FP)
    预测成功率(Accuracy, Acc):
            Acc = (TP + TN) / (TP + FP + TN + FN)
    Matthew 相关系数(Matthew's correlation coefficient, Mcc):
        MCC = (TP*TN- FP*FN)/sqrt((TP + FP)*(TN + FN)*(TP + FN)*(TN + FP)).其中sqrt代表开平方.
"""
              
def save_report(metric, cm, labels, report_file):
    accl, snl, spl, ppvl, mccl = metric
    with open(report_file, "w") as f:
        tp, fn, fp, tn, sn, sp, acc, mcc, ppv = ("tp",
           "fn", "fp", "tn", "sn", "sp", "acc", "mcc", "ppv")
        line0 = f"   {tp:<4}{fn:<4}{fp:<4}{tn:<4}{sn:<7}{sp:<7}{ppv:<7}{acc:<7}{mcc:<7}\n"
        f.write(line0)
        for idx, line in enumerate(cm):
            (tn, fp), (fn, tp) = line
            acc, sn, sp, ppv, mcc = accl[idx]*100, snl[idx]*100, spl[idx]*100, ppvl[idx]*100, mccl[idx]*100
            linei = f"{idx:<3}{tp:<4}{fn:<4}{fp:<4}{tn:<4}{sn:<7.2f}{sp:<7.2f}{ppv:<7.2f}{acc:<7.2f}{mcc:<7.2f}\n"
            f.write(linei)
        f.write("\n\n")
        f.write(TEXT)
        f.write("\n\n")
        for l in zip(*labels):
            f.write(",".join([str(i) for i in l]))
            f.write("\n")

def exist_file(*file_path):
    for file in file_path:
        f = Path(file)
        if f.is_file():
            pass
        else:
            print("file not found!")
            exit()

def heatmap_font_size(types):
    if types <= 10:
        annot_size = 4
        tick_size = 4
        label_size = 5
    elif types <= 20:
        annot_size = 3
        tick_size = 3
        label_size = 4     
    elif types <= 30:
        annot_size = 2.5
        tick_size = 2.5
        label_size = 3.5 
    elif types <= 40:
        annot_size = 2.5
        tick_size = 2.5
        label_size = 3.5 
    elif types <= 50:
        annot_size = 1.5
        tick_size = 2.5
        label_size = 3.5 
    elif types <= 60:
        annot_size = 1.5
        tick_size = 2.5
        label_size = 3.5 
    else:
        annot_size = 1
        tick_size = 2
        label_size = 3 
    return annot_size, tick_size, label_size

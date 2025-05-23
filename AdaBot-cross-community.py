import torch
import numpy as np
from numpy import cos,sin,pi
from torch import nn
from torch_geometric.nn import RGCNConv
from torch_geometric.loader import NeighborLoader
from torch_geometric.data import Data
from sklearn.metrics import f1_score,precision_score,accuracy_score,recall_score
import time
import torch.nn.functional as F
import contextlib
import argparse
from copy import deepcopy
from sys import stderr
from collections import OrderedDict
from torch_geometric.transforms import RandomNodeSplit


parser = argparse.ArgumentParser(description="Parameter Processing")

# BASIC
parser.add_argument('--data_root_path', type=str,default="./Pre_Data/T_22_com/")
parser.add_argument('--save_path', type=str)
parser.add_argument('--coms', type=list,default=[[5,6],[6,5]])
parser.add_argument('--exp_times', type=int, default=5)
parser.add_argument('--device', type=str, default="cuda:0")
parser.add_argument('--train_report',type=int,default=100)
parser.add_argument('--test_report',type=int,default=500)
parser.add_argument('--train_ratio',type=float,default=0.7)
parser.add_argument('--eval_ratio',type=float,default=0.2)

parser.add_argument('--exp_all',type=bool,default=True)

# HYPERPARAMETER
parser.add_argument('--lr', type=float, default=1e-4)
parser.add_argument('--L2_reg', type=float, default=1e-3)
parser.add_argument('--dropout', type=float, default=0.5)
parser.add_argument('--hidden_size', type=int, default=128)
parser.add_argument('--iterations', type=int, default=5_000) # iterations = epochs*(data_nums/batch_size)
parser.add_argument('--transformer_att_head', type=int, default=2)
parser.add_argument('--batch_size', type=int, default=512)
parser.add_argument('--text_input_size', type=int, default=768)
parser.add_argument('--meta_input_size', type=int, default=8)
parser.add_argument('--num_relations', type=int, default=2)
parser.add_argument('--lmd_dis', type=float, default=1.0)
parser.add_argument('--lmd_cet', type=float, default=0.005)
parser.add_argument('--lmd_vat', type=float, default=0.001)
parser.add_argument('--ema_decay', type=float, default=0.999)
parser.add_argument('--ema_interval', type = int, default=1)
parser.add_argument('--meta_align',type=bool,default=True)
parser.add_argument('--ssa',type=bool,default=True) #Source Signal Annealing
parser.add_argument('--ssa_schedule',type=str,default="sin",help="linear, cos, sin")

args = parser.parse_args()
if args.exp_all:
    coms = []
    for i in range(5,10):
        for j in range(5,10):
            if i == j:
                continue
            else:
                coms.append([i,j])
    for i in range(5):
        for j in range(5):
            if i == j:
                continue
            else:
                coms.append([i,j])
    args.coms = coms
print(args)
################################################################

def get_TwiBot22Com_Dataset(data_root_path,com):

    label = torch.load(data_root_path+f"com{com}_label.pt",map_location=args.device)
    text = torch.load(data_root_path+f"com{com}_text.pt",map_location=args.device)
    meta = torch.load(data_root_path+f"com{com}_meta.pt",map_location=args.device)
    edge_index = torch.load(data_root_path+f"com{com}_edge_index.pt",map_location=args.device)
    edge_type = torch.load(data_root_path+f"com{com}_edge_type.pt",map_location=args.device)
    num_nodes = label.shape[0]

    print(f"Com: {com}")
    print("Label:",label.shape)
    print("Meta:",meta.shape)
    print("Text:",text.shape)
    print("Edge_index:",edge_index.shape)
    print("Edge_type:",edge_type.shape)

    edge_type[edge_type==12] = 1
    return Data(edge_index=edge_index,
                y=label,
                text=text,
                meta=meta,
                edge_type=edge_type,
                num_nodes=num_nodes)

#################################################################################
class EMA(nn.Module):
    def __init__(self,model:nn.Module, decay:float):
        super().__init__()
        self.decay = decay
        self.model = model
        self.shadow = deepcopy(self.model)
        for param in self.shadow.parameters():
            param.detach_()
    
    @torch.no_grad()
    def update(self):
        if not self.training:
            print("EMA update should only be called during training", file=stderr, flush=True)
            return
        
        model_params = OrderedDict(self.model.named_parameters())
        shadow_params = OrderedDict(self.shadow.named_parameters())
        assert model_params.keys() == shadow_params.keys()

        for name,param in model_params.items():
            shadow_params[name].sub_((1.-self.decay)*(shadow_params[name]-param))
        
        model_buffers = OrderedDict(self.model.named_buffers())
        shadow_buffers = OrderedDict(self.shadow.named_buffers())
        assert model_buffers.keys() == shadow_buffers.keys()

        for name,buffer in model_buffers.items():
            shadow_buffers[name].copy_(buffer)

    def forward(self, inputs):
        if self.training:
            return self.model(inputs)
        else:
            return self.shadow(inputs)
#################################################################################

class RGCN_Encoder(nn.Module):
    def __init__(self,hidden_size,num_relations,dropout):
        super(RGCN_Encoder,self).__init__()
        self.gcn1 = RGCNConv(hidden_size*2,hidden_size,num_relations)
        self.gcn2 = RGCNConv(hidden_size,hidden_size,num_relations)
        self.relu = nn.ReLU()
        self.dropout = nn.Dropout(dropout)

    def forward(self,x,edge_index,edge_type):
        graph_feature = torch.cat(x,dim=1)
        graph_feature = self.gcn1(graph_feature,edge_index,edge_type) 
        graph_feature = self.dropout(self.relu(graph_feature))
        graph_feature = self.gcn2(graph_feature,edge_index,edge_type)
        graph_feature = self.dropout(self.relu(graph_feature))
        return graph_feature

##############################################################################
class MLP_2L(nn.Module):
    def __init__(self,input_size,hidden_size,output_size,dropout):
        super(MLP_2L,self).__init__()
        self.linear1 = nn.Linear(input_size,hidden_size)
        self.linear2 = nn.Linear(hidden_size,output_size)
        self.relu = nn.ReLU()
        self.dropout = nn.Dropout(dropout)

    def forward(self,x):
        feature = self.linear1(x)
        feature = self.relu(feature)
        feature = self.linear2(feature)
        feature = self.relu(feature)
        feature = self.dropout(feature)
        return feature
##############################################################################################
class ABot_Feature_Generator(nn.Module):
    def __init__(self,hidden_size,text_input_size,meta_input_size,dropout,num_relations,transformer_att_head):
        super(ABot_Feature_Generator,self).__init__()
        self.graph_encoder = RGCN_Encoder(hidden_size,num_relations,dropout)
        self.text_encoder = MLP_2L(text_input_size,hidden_size,hidden_size,dropout)
        self.meta_encoder = MLP_2L(meta_input_size,hidden_size,hidden_size,dropout)
        self.relu = nn.ReLU()
        self.TRM = torch.nn.MultiheadAttention(hidden_size,transformer_att_head)
        self.con_linear = nn.Linear(3*3,hidden_size)

    def forward(self,input):
        x,edge_index,edge_type = input
        meta,text = x[0],x[1]
        #description [batch_size,token_len,768]

        meta_feature = self.meta_encoder(meta)
        text_feature = self.text_encoder(text)

        graph_feature = self.graph_encoder([meta_feature.detach().clone(),text_feature.detach().clone()],edge_index,edge_type)


        feature = torch.stack((graph_feature,text_feature,meta_feature),0) #[3,batch_size,hidden_size]
        feature,att_w = self.TRM(feature,feature,feature)
        graph_feature,text_feature,meta_feature = torch.split(feature,1)
        graph_feature = graph_feature.squeeze(0)
        text_feature = text_feature.squeeze(0)
        meta_feature = meta_feature.squeeze(0)

        feature_con = att_w.view(att_w.shape[0],3*3)
        feature_con = self.con_linear(feature_con)
        final_feature = torch.cat([graph_feature,text_feature,meta_feature,feature_con],dim=1)
        return final_feature
    

class ABot_Classifier(nn.Module):
    def __init__(self,hidden_size,dropout):
        super(ABot_Classifier,self).__init__()
        self.relu = nn.ReLU()
        self.output_linear1 = nn.Linear(hidden_size*4,hidden_size)
        self.output_linear2 = nn.Linear(hidden_size,2)
        self.softmax = nn.Softmax(dim=1)
        self.batch_norm = nn.BatchNorm1d(hidden_size)
        self.dropout = nn.Dropout(dropout)

    def forward(self,input):
        feature = input
        final_feature = self.relu(self.output_linear1(feature))
        final_feature = self.batch_norm(self.dropout(final_feature))
        final_feature = self.output_linear2(final_feature)
        prob = self.softmax(final_feature)
        return prob

################################################################################################

# sliced wasserstein computation use
def get_theta(embedding_dim, num_samples=50):
    theta = [w / np.sqrt((w ** 2).sum())
             for w in np.random.normal(size=(num_samples, embedding_dim))]
    theta = np.asarray(theta)
    return torch.from_numpy(theta).type(torch.FloatTensor).to(args.device)


def sliced_wasserstein_distance(source_z, target_z, embed_dim, num_projections=256, p=1):
    # \theta is vector represents the projection directoin
    theta = get_theta(embed_dim, num_projections) #[num_projections,embed_dim]
    proj_target = target_z.matmul(theta.transpose(0, 1))#[batch_size,num_projections]
    proj_source = source_z.matmul(theta.transpose(0, 1))
    w_distance = torch.sort(proj_target.transpose(0, 1), dim=1)[0] - torch.sort(proj_source.transpose(0, 1), dim=1)[0] 

    w_distance_p = torch.pow(w_distance, p)

    return w_distance_p.mean()
################################################################################################
def conditional_entropy_loss(pred):
    mask = pred.ge(0.000001)
    mask_out = torch.masked_select(pred, mask)
    entropy = -(torch.sum(mask_out * torch.log(mask_out)))
    return entropy / float(pred.size(0))
################################################################################################
@contextlib.contextmanager
def _disable_tracking_bn_stats(model):

    def switch_attr(m):
        if hasattr(m, 'track_running_stats'):
            m.track_running_stats ^= True
            
    model.apply(switch_attr)
    yield
    model.apply(switch_attr)

def _l2_normalize(d):
    d_reshaped = d.view(d.shape[0], -1, *(1 for _ in range(d.dim() - 2)))
    d /= torch.norm(d_reshaped, dim=1, keepdim=True) + 1e-8
    return d

class VATLoss(nn.Module):

    def __init__(self, xi=10.0, eps=1.0, ip=1):
        """VAT loss
        :param xi: hyperparameter of VAT (default: 10.0)
        :param eps: hyperparameter of VAT (default: 1.0)
        :param ip: iteration times of computing adv noise (default: 1)
        """
        super(VATLoss, self).__init__()
        self.xi = xi
        self.eps = eps
        self.ip = ip

    def forward(self, model_f,model_c, x):
        with torch.no_grad():
            pred = model_f([[x.meta,x.text],x.edge_index,x.edge_type])
            pred = pred[:x.batch_size]
            pred = model_c(pred)

        # prepare random unit tensor
        d_m = torch.rand(x.meta.shape).sub(0.5).to(x.meta.device)
        d_m = _l2_normalize(d_m)
        d_t = torch.rand(x.text.shape).sub(0.5).to(x.text.device)
        d_t = _l2_normalize(d_t)

        with _disable_tracking_bn_stats(model_f):
            with _disable_tracking_bn_stats(model_c):
                # calc adversarial direction
                for _ in range(self.ip):
                    d_m.requires_grad_()
                    d_t.requires_grad_()
                    pred_hat = model_f([[x.meta+self.xi*d_m, x.text+self.xi*d_t],x.edge_index,x.edge_type])
                    pred_hat = pred_hat[:x.batch_size]
                    logp_hat = model_c(pred_hat)
                    adv_distance = F.kl_div(logp_hat, pred, reduction='batchmean')
                    adv_distance.backward()
                    d_m = _l2_normalize(d_m.grad)
                    d_t = _l2_normalize(d_t.grad)
                    model_f.zero_grad()
                    model_c.zero_grad()
        
                # calc LDS
                r_adv_m = d_m * self.eps
                r_adv_t = d_t * self.eps
                pred_hat = model_f([[x.meta+r_adv_m,x.text+r_adv_t],x.edge_index,x.edge_type])
                pred_hat = pred_hat[:x.batch_size]
                logp_hat = model_c(pred_hat)
                lds = F.kl_div(logp_hat, pred, reduction='batchmean')

        return lds

################################################################################################
def train_loop(src_batch,tgt_batch,model_f,model_c,criterion,optimizer_f,optimizer_c,ssa_ratio):
    model_f.train()
    model_c.train()

    train_loss = 0

    src_n_batch = src_batch.batch_size
    tgt_n_batch = tgt_batch.batch_size
    src_feature = model_f([[src_batch.meta,src_batch.text],src_batch.edge_index,src_batch.edge_type])
    tgt_feature = model_f([[tgt_batch.meta,tgt_batch.text],tgt_batch.edge_index,tgt_batch.edge_type])

    dis_n = min(src_n_batch,tgt_n_batch) 

    # SWD domain loss
    dis_loss = sliced_wasserstein_distance(src_feature[:dis_n],tgt_feature[:dis_n],int(src_feature.shape[1]))

    src_feature = src_feature[:src_n_batch]
    src_pred = model_c(src_feature)
    src_label = src_batch.y[:src_n_batch]

    # Source Cross-Entropy Classification Loss
    cls_loss = criterion(src_pred,src_label)*ssa_ratio

    # Conditional Entropy Loss
    tgt_feature = tgt_feature[:tgt_n_batch]
    tgt_pred = model_c(tgt_feature)
    cet_loss = conditional_entropy_loss(tgt_pred)

    # Virtual Adversarial Training (VAT)
    vat_fn = VATLoss()
    src_vat_loss = vat_fn(model_f,model_c,src_batch)*ssa_ratio
    tgt_vat_loss = vat_fn(model_f,model_c,tgt_batch)
    vat_loss = src_vat_loss+tgt_vat_loss

    loss = cls_loss+dis_loss*args.lmd_dis+cet_loss*args.lmd_cet+vat_loss*args.lmd_vat

    train_loss += cls_loss.item()*src_n_batch 
    train_loss += dis_loss.item()*args.lmd_dis
    train_loss += cet_loss.item()*args.lmd_cet
    train_loss += vat_loss.item()*args.lmd_vat


    optimizer_f.zero_grad()
    optimizer_c.zero_grad()
    loss.backward()
    optimizer_c.step()
    optimizer_f.step()

    return train_loss


def test_loop(dataloader,model_f,model_c,criterion):
    model_f.eval()
    model_c.eval()

    test_loss, correct, cnt = 0, 0, 0
    prob_all = []
    label_all = []

    with torch.no_grad():
        for batch in dataloader:
            n_batch = batch.batch_size 
            feature = model_f([[batch.meta,batch.text],batch.edge_index,batch.edge_type])
            pred = model_c(feature)

            pred = pred[:n_batch]
            label = batch.y[:n_batch]
            loss = criterion(pred,label) 

            label = label.detach().cpu()
            pred = pred.detach().cpu()
            test_loss += loss.item()*n_batch 
            cnt+=n_batch
            correct += (pred.argmax(1) == label).type(torch.float).sum().item()
            prob_all.extend(np.argmax(pred,axis=1))
            label_all.extend(label)

    test_loss /= cnt
    correct /= cnt
    f1 = f1_score(label_all,prob_all)
    print(f"Test Accuracy: {(100*correct):>0.2f}%, F1-Score: {f1:>0.4f}, Avg loss: {test_loss:>8f} \n")
    return correct,f1

def get_time():
    return str(time.strftime("[%Y-%m-%d %H:%M:%S]", time.localtime()))

for src_com,tgt_com in args.coms:
    crt = [0]*args.exp_times
    f1 = [0]*args.exp_times
    print(get_time()+f"^^^^^^^^Source Com {src_com}, Target Com {tgt_com}^^^^^^^^")
    src_dataset = get_TwiBot22Com_Dataset(args.data_root_path,src_com)
    src_loader = NeighborLoader(src_dataset,num_neighbors=[256,256],batch_size=args.batch_size,shuffle=True)
    len_src = len(src_loader)
    tgt_dataset = get_TwiBot22Com_Dataset(args.data_root_path,tgt_com)
    if args.meta_align:
        print("Target Meta Data Aligning with Source Meta Data")
        sm_mean = torch.load(args.data_root_path+f"com{src_com}_meta_mean.pt",map_location=args.device)
        sm_std = torch.load(args.data_root_path+f"com{src_com}_meta_std.pt",map_location=args.device)
        tm_mean = torch.load(args.data_root_path+f"com{tgt_com}_meta_mean.pt",map_location=args.device)
        tm_std = torch.load(args.data_root_path+f"com{tgt_com}_meta_std.pt",map_location=args.device)
        tgt_dataset.meta = ((tgt_dataset.meta*tm_std)+tm_mean-sm_mean)/sm_std

    # Tgt Dataset Split
    random_node_split = RandomNodeSplit(num_val=args.eval_ratio,num_test=1-args.train_ratio-args.eval_ratio)
    tgt_dataset = random_node_split(tgt_dataset)
    print(f"Test Num {tgt_dataset.test_mask.sum()} Train Num {tgt_dataset.train_mask.sum()}")
    tgt_train_loader = NeighborLoader(tgt_dataset,num_neighbors=[256,256],batch_size=args.batch_size,shuffle=True,input_nodes=tgt_dataset.train_mask)
    tgt_test_loader = NeighborLoader(tgt_dataset,num_neighbors=[256,256],batch_size=args.batch_size,shuffle=True,input_nodes=tgt_dataset.test_mask)        
    len_tgt = len(tgt_train_loader)
    for e in range(args.exp_times):
        print(get_time()+f"^^^^^^^^EXP{e}^^^^^^^^")
        ini_model_f = ABot_Feature_Generator(args.hidden_size,args.text_input_size,args.meta_input_size,args.dropout,args.num_relations,args.transformer_att_head).to(args.device)
        ini_model_c = ABot_Classifier(args.hidden_size,args.dropout).to(args.device)
        ema_model_f = EMA(ini_model_f,args.ema_decay)
        ema_model_c = EMA(ini_model_c,args.ema_decay)

        criterion = nn.CrossEntropyLoss()
        optimizer_f = torch.optim.Adam(ema_model_f.parameters(),lr=args.lr,weight_decay=args.L2_reg)
        optimizer_c = torch.optim.Adam(ema_model_c.parameters(),lr=args.lr,weight_decay=args.L2_reg)

        for t in range(args.iterations):
            if t % len_src == 0: 
                itr_src = iter(src_loader)
            if t % len_tgt == 0:
                itr_tgt = iter(tgt_train_loader)
            
            scr_batch = next(itr_src)
            tgt_batch = next(itr_tgt)

            if not args.ssa:
                ssa_ratio = 1.0
            else:
                if args.ssa_schedule == 'linear':
                    ssa_ratio = 1.0-(t/args.iterations)
                elif args.ssa_schedule == 'cos':
                    ssa_ratio = cos(t/args.iterations*(pi/2))
                elif args.ssa_schedule == 'sin':
                    ssa_ratio = 1.0-sin(t/args.iterations*(pi/2))
                else:
                    raise KeyError
            
            train_loss = train_loop(scr_batch, tgt_batch, ema_model_f,ema_model_c,criterion, optimizer_f,optimizer_c,ssa_ratio)
            if t % args.train_report == 0:
                print(get_time(),f"Iteration {t}::Train loss {train_loss:>8f}")
            if (t+1) % args.ema_interval == 0:
                ema_model_c.update()
                ema_model_f.update()
            if (t+1) % args.test_report == 0:
                print(f"******Iteration-{t+1}******")
                print(f"Test on Com {tgt_com} ######")
                crt[e],f1[e]=test_loop(tgt_test_loader,ema_model_f,ema_model_c,criterion)

        # torch.save(model.state_dict(),save_path+f"model_com{train_com}.pth")

        del ini_model_f
        del ini_model_c
        del ema_model_c
        del ema_model_f
        del criterion
        del optimizer_f
        del optimizer_c

    del tgt_dataset
    del tgt_test_loader
    del tgt_train_loader
    del src_dataset
    del src_loader

    tmp_c = np.array(crt,dtype=np.float32)
    tmp_f = np.array(f1,dtype=np.float32)
    print(f"@@@@@@ Source Com {src_com}, Target Com {tgt_com}: Mean ACC {(100*tmp_c.mean()):>0.2f}%, Std ACC {(100*tmp_c.std()):>0.2f}%; Mean F1 {tmp_f.mean():>0.4f}, Std F1 {tmp_f.std():>0.4f}")

print("Done!")

import numpy as np
import torch
import torch.nn as nn
from torch.autograd import Function
from torch.nn.modules.module import Module
from torch.nn.functional import fold, unfold
from torchvision.utils import make_grid
import math

from utils import resize_image
import custom_transforms as transforms
from custom_blocks import PatchEmbed, TransformerBlock, trunc_normal_


#################################################################################
# You will need to fill in the missing code in this file
#################################################################################


#################################################################################
# Part I: Understanding Convolutions
#################################################################################
class CustomConv2DFunction(Function):
    @staticmethod
    def forward(ctx, input_feats, weight, bias, stride=1, padding=0):
        """
        Forward propagation of convolution operation.
        We only consider square filters with equal stride/padding in width and height!

        Args:
          input_feats: input feature map of size N * C_i * H * W
          weight: filter weight of size C_o * C_i * K * K
          bias: (optional) filter bias of size C_o
          stride: (int, optional) stride for the convolution. Default: 1
          padding: (int, optional) Zero-padding added to both sides of the input. Default: 0

        Outputs:
          output: responses of the convolution  w*x+b

        """
        # sanity check
        assert weight.size(2) == weight.size(3)
        assert input_feats.size(1) == weight.size(1)
        assert isinstance(stride, int) and (stride > 0)
        assert isinstance(padding, int) and (padding >= 0)

        # save the conv params
        kernel_size = weight.size(2)
        ctx.stride = stride
        ctx.padding = padding
        ctx.input_height = input_feats.size(2)
        ctx.input_width = input_feats.size(3)

        # make sure this is a valid convolution
        assert kernel_size <= (input_feats.size(2) + 2 * padding)
        assert kernel_size <= (input_feats.size(3) + 2 * padding)

        #################################################################################
        # Fill in the code here
        #################################################################################

        input_feats_unfold = unfold(input_feats, kernel_size=(kernel_size,kernel_size), padding=padding, stride=stride)
        weight_unfold = weight.view(weight.size(0), -1)

        out_unfold = input_feats_unfold.transpose(1,2).matmul(weight_unfold.t()).transpose(1,2)
        bias_unfold = bias.expand(out_unfold.size()[:2]).unsqueeze(2).expand(out_unfold.size())
        out_unfold = out_unfold+bias_unfold

        out_dim1 = ((ctx.input_height + 2 * ctx.padding - kernel_size)//stride)+1
        out_dim2 = ((ctx.input_width + 2 * ctx.padding - kernel_size)//stride)+1

        out_fold = fold(out_unfold, output_size=(out_dim1, out_dim2), kernel_size=(1,1), stride=1)

        # save for backward (you need to save the unfolded tensor into ctx)
        # ctx.save_for_backward(your_vars, weight, bias)
        ctx.save_for_backward(input_feats_unfold, weight_unfold, weight, bias)

        return out_fold

    @staticmethod
    def backward(ctx, grad_output):
        """
        Backward propagation of convolution operation

        Args:
          grad_output: gradients of the outputs

        Outputs:
          grad_input: gradients of the input features
          grad_weight: gradients of the convolution weight
          grad_bias: gradients of the bias term

        """
        # unpack tensors and initialize the grads
        input_feats_unfold, weight_unfold, weight, bias = ctx.saved_tensors
        grad_input = grad_weight = grad_bias = None

        # recover the conv params
        kernel_size = weight.size(2)
        stride = ctx.stride
        padding = ctx.padding
        input_height = ctx.input_height
        input_width = ctx.input_width

        #################################################################################
        # Fill in the code here
        #################################################################################
        # compute the gradients w.r.t. input and params
        grad_output_unfold = unfold(grad_output, kernel_size=(1,1), stride=1)

        grad_input_unfold = grad_output_unfold.transpose(1,2).matmul(weight_unfold).transpose(1,2)
        grad_input = fold(grad_input_unfold,output_size=(input_height,input_width),kernel_size=(kernel_size,kernel_size),padding=padding,stride=stride)

        grad_weight_unfold = input_feats_unfold.matmul(grad_output_unfold.transpose(1,2)).transpose(1,2)
        grad_weight = grad_weight_unfold.sum((0)).view(grad_weight_unfold.size(1),-1,kernel_size,kernel_size)

        if bias is not None and ctx.needs_input_grad[2]:
            # compute the gradients w.r.t. bias (if any)
            grad_bias = grad_output.sum((0, 2, 3))

        return grad_input, grad_weight, grad_bias, None, None


custom_conv2d = CustomConv2DFunction.apply


class CustomConv2d(Module):
    """
    The same interface as torch.nn.Conv2D
    """

    def __init__(
        self,
        in_channels,
        out_channels,
        kernel_size,
        stride=1,
        padding=0,
        dilation=1,
        groups=1,
        bias=True,
    ):
        super(CustomConv2d, self).__init__()
        assert isinstance(kernel_size, int), "We only support squared filters"
        assert isinstance(stride, int), "We only support equal stride"
        assert isinstance(padding, int), "We only support equal padding"
        self.in_channels = in_channels
        self.out_channels = out_channels
        self.kernel_size = kernel_size
        self.stride = stride
        self.padding = padding

        # not used (for compatibility)
        self.dilation = dilation
        self.groups = groups

        # register weight and bias as parameters
        self.weight = nn.Parameter(
            torch.Tensor(out_channels, in_channels, kernel_size, kernel_size)
        )
        if bias:
            self.bias = nn.Parameter(torch.Tensor(out_channels))
        else:
            self.register_parameter("bias", None)
        self.reset_parameters()

    def reset_parameters(self):
        # initialization using Kaiming uniform
        nn.init.kaiming_uniform_(self.weight, a=math.sqrt(5))
        if self.bias is not None:
            fan_in, _ = nn.init._calculate_fan_in_and_fan_out(self.weight)
            bound = 1 / math.sqrt(fan_in)
            nn.init.uniform_(self.bias, -bound, bound)

    def forward(self, input):
        # call our custom conv2d op
        return custom_conv2d(input, self.weight, self.bias, self.stride, self.padding)

    def extra_repr(self):
        s = (
            "{in_channels}, {out_channels}, kernel_size={kernel_size}"
            ", stride={stride}, padding={padding}"
        )
        if self.bias is None:
            s += ", bias=False"
        return s.format(**self.__dict__)


#################################################################################
# Part II: Design and train a network
#################################################################################
class SimpleNet(nn.Module):
    # a simple CNN for image classifcation
    def __init__(self, conv_op=nn.Conv2d, num_classes=100):
        super(SimpleNet, self).__init__()
        # you can start from here and create a better model
        self.features = nn.Sequential(
            # conv1 block: conv 7x7
            conv_op(3, 64, kernel_size=7, stride=2, padding=3),
            nn.ReLU(inplace=True),
            # max pooling 1/2
            nn.MaxPool2d(kernel_size=3, stride=2, padding=1),
            # conv2 block: simple bottleneck
            conv_op(64, 64, kernel_size=1, stride=1, padding=0),
            nn.ReLU(inplace=True),
            conv_op(64, 64, kernel_size=3, stride=1, padding=1),
            nn.ReLU(inplace=True),
            conv_op(64, 256, kernel_size=1, stride=1, padding=0),
            nn.ReLU(inplace=True),
            # max pooling 1/2
            nn.MaxPool2d(kernel_size=3, stride=2, padding=1),
            # conv3 block: simple bottleneck
            conv_op(256, 128, kernel_size=1, stride=1, padding=0),
            nn.ReLU(inplace=True),
            conv_op(128, 128, kernel_size=3, stride=1, padding=1),
            nn.ReLU(inplace=True),
            conv_op(128, 512, kernel_size=1, stride=1, padding=0),
            nn.ReLU(inplace=True),
        )
        # global avg pooling + FC
        self.avgpool = nn.AdaptiveAvgPool2d((1, 1))
        self.fc = nn.Linear(512, num_classes)

    def reset_parameters(self):
        # init all params
        for m in self.modules():
            if isinstance(m, nn.Conv2d):
                nn.init.kaiming_normal_(m.weight, mode="fan_out", nonlinearity="relu")
                if m.bias is not None:
                    nn.init.consintat_(m.bias, 0.0)
            elif isinstance(m, nn.BatchNorm2d):
                nn.init.constant_(m.weight, 1.0)
                nn.init.constant_(m.bias, 0.0)

    def forward(self, x):
        # you can implement adversarial training here
        # if self.training:
        #   # generate adversarial sample based on x
        x = self.features(x)
        x = self.avgpool(x)
        x = x.view(x.size(0), -1)
        x = self.fc(x)
        return x


class SimpleViT(nn.Module):
    """
    This module implements Vision Transformer (ViT) backbone in
    "Exploring Plain Vision Transformer Backbones for Object Detection",
    https://arxiv.org/abs/2203.16527
    """

    def __init__(
        self,
        img_size=128,
        num_classes=100,
        patch_size=16,
        in_chans=3,
        embed_dim=192,
        depth=4,
        num_heads=4,
        mlp_ratio=4.0,
        qkv_bias=True,
        drop_path_rate=0.1,
        norm_layer=nn.LayerNorm,
        act_layer=nn.GELU,
        use_abs_pos=True,
        window_size=4,
        window_block_indexes=(1, 3),
    ):
        """
        Args:
            img_size (int): Input image size.
            num_classes (int): Number of object categories
            patch_size (int): Patch size.
            in_chans (int): Number of input image channels.
            embed_dim (int): Patch embedding dimension.
            depth (int): Depth of ViT.
            num_heads (int): Number of attention heads in each ViT block.
            mlp_ratio (float): Ratio of mlp hidden dim to embedding dim.
            qkv_bias (bool): If True, add a learnable bias to query, key, value.
            drop_path_rate (float): Stochastic depth rate.
            norm_layer (nn.Module): Normalization layer.
            act_layer (nn.Module): Activation layer.
            use_abs_pos (bool): If True, use absolute positional embeddings.
            window_size (int): Window size for window attention blocks.
            window_block_indexes (list): Indexes for blocks using window attention.
            E.g., [0, 2] indicates the first and the third blocks will use window attention.

        Feel free to modify the default parameters here.
        """
        super(SimpleViT, self).__init__()

        if use_abs_pos:
            # Initialize absolute positional embedding with image size
            # The embedding is learned from data
            self.pos_embed = nn.Parameter(
                torch.zeros(
                    1, img_size // patch_size, img_size // patch_size, embed_dim
                )
            )
        else:
            self.pos_embed = None

        # stochastic depth decay rule
        dpr = [x.item() for x in torch.linspace(0, drop_path_rate, depth)]

        ########################################################################
        # Fill in the code here
        ########################################################################
        # the implementation shall start from embedding patches,
        # followed by some transformer blocks
        self.embed_layer = PatchEmbed(kernel_size=(patch_size,patch_size),
                                      stride=(patch_size,patch_size),
                                      in_chans=in_chans,
                                      embed_dim=embed_dim)

        transformer_seq = [TransformerBlock(dim=embed_dim,
                                            num_heads=num_heads,
                                            mlp_ratio=mlp_ratio,
                                            qkv_bias=qkv_bias,
                                            drop_path=drop_path_,
                                            norm_layer=norm_layer,
                                            act_layer=act_layer,
                                            window_size=window_size if layer in window_block_indexes else 0) 
                                            for layer, drop_path_ in enumerate(dpr)]
        self.transformers = torch.nn.Sequential(*transformer_seq)

        self.avgpool = nn.AdaptiveAvgPool3d((1, 1, embed_dim))
        self.fc = nn.Linear(embed_dim, num_classes)

        if self.pos_embed is not None:
            trunc_normal_(self.pos_embed, std=0.02)

        self.apply(self._init_weights)
        # add any necessary weight initialization here

    def _init_weights(self, m):
        if isinstance(m, nn.Linear):
            trunc_normal_(m.weight, std=0.02)
            if isinstance(m, nn.Linear) and m.bias is not None:
                nn.init.constant_(m.bias, 0)
        elif isinstance(m, nn.LayerNorm):
            nn.init.constant_(m.bias, 0)
            nn.init.constant_(m.weight, 1.0)

    def forward(self, x):
        ########################################################################
        # Fill in the code here
        ########################################################################
        x = self.embed_layer(x)
        if self.pos_embed is not None:
            pos_embed = self.pos_embed.expand(x.size())
            x = x+pos_embed
        x = self.transformers(x)
        x = self.avgpool(x)
        x = x.view(x.size(0),-1)
        x = self.fc(x)
        return x


# change this to your model!
default_cnn_model = SimpleNet
default_vit_model = SimpleViT

# define data augmentation used for training, you can tweak things if you want
def get_train_transforms(normalize):
    train_transforms = []
    train_transforms.append(transforms.Scale(144))
    train_transforms.append(transforms.RandomHorizontalFlip())
    train_transforms.append(transforms.RandomColor(0.15))
    train_transforms.append(transforms.RandomRotate(15))
    train_transforms.append(transforms.RandomSizedCrop(128))
    train_transforms.append(transforms.ToTensor())
    train_transforms.append(normalize)
    train_transforms = transforms.Compose(train_transforms)
    return train_transforms


# define data augmentation used for validation, you can tweak things if you want
def get_val_transforms(normalize):
    val_transforms = []
    val_transforms.append(transforms.Scale(144))
    val_transforms.append(transforms.CenterCrop(128))
    val_transforms.append(transforms.ToTensor())
    val_transforms.append(normalize)
    val_transforms = transforms.Compose(val_transforms)
    return val_transforms


#################################################################################
# Part III: Adversarial samples and Attention
#################################################################################
class PGDAttack(object):
    def __init__(self, loss_fn, num_steps=10, step_size=0.01, epsilon=0.1):
        """
        Attack a network by Project Gradient Descent. The attacker performs
        k steps of gradient descent of step size a, while always staying
        within the range of epsilon (under l infinity norm) from the input image.

        Args:
          loss_fn: loss function used for the attack
          num_steps: (int) number of steps for PGD
          step_size: (float) step size of PGD (i.e., alpha in our lecture)
          epsilon: (float) the range of acceptable samples
                   for our normalization, 0.1 ~ 6 pixel levels
        """
        self.loss_fn = loss_fn
        self.num_steps = num_steps
        self.step_size = step_size
        self.epsilon = epsilon

    def perturb(self, model, input):
        """
        Given input image X (torch tensor), return an adversarial sample
        (torch tensor) using PGD of the least confident label.

        See https://openreview.net/pdf?id=rJzIBfZAb

        Args:
          model: (nn.module) network to attack
          input: (torch tensor) input image of size N * C * H * W

        Outputs:
          output: (torch tensor) an adversarial sample of the given network
        """
        # clone the input tensor and disable the gradients
        output = input.clone()
        input.requires_grad = False

        # loop over the number of steps
        #################################################################################
        # Fill in the code here
        #################################################################################
        for params in model.parameters():
            params.requires_grad = False
        
        output.requires_grad = True
        if output.grad is not None:
            output.grad.zero_()
        
        for _ in range(self.num_steps):
            output.requires_grad = True
            if output.grad is not None:
                output.grad.zero_()

            softmax_conf = model(output)
            least_conf = softmax_conf.argmin(1)

            least_conf_loss = self.loss_fn(softmax_conf,least_conf)
            least_conf_loss.backward()

            adv_img = output + self.step_size*output.grad.data.sign()
            output = torch.clamp(adv_img, min=input-self.epsilon, max=input+self.epsilon).detach()

        return output


default_attack = PGDAttack


class GradAttention(object):
    def __init__(self, loss_fn):
        """
        Visualize a network's decision using gradients

        Args:
          loss_fn: loss function used for the attack
        """
        self.loss_fn = loss_fn

    def explain(self, model, input):
        """
        Given input image X (torch tensor), return a saliency map
        (torch tensor) by computing the max of abs values of the gradients
        given by the predicted label

        See https://arxiv.org/pdf/1312.6034.pdf

        Args:
          model: (nn.module) network to attack
          input: (torch tensor) input image of size N * C * H * W

        Outputs:
          output: (torch tensor) a saliency map of size N * 1 * H * W
        """
        # make sure input receive grads
        input.requires_grad = True
        if input.grad is not None:
            input.grad.zero_()

        #################################################################################
        # Fill in the code here
        #################################################################################
        for params in model.parameters():
            params.requires_grad = False

        model.eval()

        softmax_conf = model(input)
        most_conf = softmax_conf.argmax(1)

        most_conf_loss = self.loss_fn(softmax_conf,most_conf)
        most_conf_loss.backward()

        saliency, _ = input.grad.data.abs().max(1)

        return saliency.unsqueeze(1)


default_attention = GradAttention


def vis_grad_attention(input, vis_alpha=2.0, n_rows=10, vis_output=None):
    """
    Given input image X (torch tensor) and a saliency map
    (torch tensor), compose the visualziations

    Args:
      input: (torch tensor) input image of size N * C * H * W
      output: (torch tensor) input map of size N * 1 * H * W

    Outputs:
      output: (torch tensor) visualizations of size 3 * HH * WW
    """
    # concat all images into a big picture
    input_imgs = make_grid(input.cpu(), nrow=n_rows, normalize=True)
    if vis_output is not None:
        output_maps = make_grid(vis_output.cpu(), nrow=n_rows, normalize=True)

        # somewhat awkward in PyTorch
        # add attention to R channel
        mask = torch.zeros_like(output_maps[0, :, :]) + 0.5
        mask = output_maps[0, :, :] > vis_alpha * output_maps[0, :, :].mean()
        mask = mask.float()
        input_imgs[0, :, :] = torch.max(input_imgs[0, :, :], mask)
    output = input_imgs
    return output


default_visfunction = vis_grad_attention

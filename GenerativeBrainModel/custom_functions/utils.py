import torch
from torch import nn
import pdb
import torch.nn.functional as F
from torch.distributions import Distribution, Independent, OneHotCategoricalStraightThrough
from torch.distributions.kl import kl_divergence
import matplotlib.pyplot as plt
import math


class STsampleMultiNom(torch.autograd.Function):
    @staticmethod
    def forward(ctx, input):
        # Store input for backward pass
        ctx.save_for_backward(input)
        
        # Sample from multinomial distribution
        x = torch.multinomial(input, 1)
        
        # Convert to one-hot encoded vector
        one_hot = torch.zeros_like(input)
        one_hot.scatter_(1, x, 1)


        return one_hot

    @staticmethod
    def backward(ctx, grad_output):
        input, = ctx.saved_tensors
        return grad_output, None

# Straight-Through Multinomial Sampler
class STMNsampler(nn.Module):
    def __init__(self):
        super(STMNsampler, self).__init__()

    def forward(self, x):
            return STsampleMultiNom.apply(x)
            
def symlog(x):
    return torch.sign(x) * torch.log(1 + torch.abs(x))

def symexp(x):
    return torch.sign(x) * (torch.exp(torch.abs(x)) - 1)

def kl_divergence_with_free_bits(q_probs, p_probs, batch_size, free_bits=1.0):
    """
    Compute KL divergence between two categorical distributions with free bits.
    
    Args:
    q_probs: Probabilities of distribution q (B, ...)
    p_probs: Probabilities of distribution p (B, ...)
    free_bits: Minimum KL divergence (default: 1.0)
    
    Returns:
    KL(q||p) for each batch element, clipped at free_bits (B,)
    """

    # Compute KL divergence
    kld = q_probs * (torch.log(q_probs) - torch.log(p_probs))

    return kld.mean()



class RMSNorm(nn.Module):
    def __init__(self, d, p=-1., eps=1e-8, bias=False):
        """
            Root Mean Square Layer Normalization
        :param d: model size
        :param p: partial RMSNorm, valid value [0, 1], default -1.0 (disabled)
        :param eps:  epsilon value, default 1e-8
        :param bias: whether use bias term for RMSNorm, disabled by
            default because RMSNorm doesn't enforce re-centering invariance.
        """
        super(RMSNorm, self).__init__()

        self.eps = eps
        self.d = d
        self.p = p
        self.bias = bias

        self.scale = nn.Parameter(torch.ones(d))
        self.register_parameter("scale", self.scale)

        if self.bias:
            self.offset = nn.Parameter(torch.zeros(d))
            self.register_parameter("offset", self.offset)

    def forward(self, x):
        if self.p < 0. or self.p > 1.:
            norm_x = x.norm(2, dim=-1, keepdim=True)
            d_x = self.d
        else:
            partial_size = int(self.d * self.p)
            partial_x, _ = torch.split(x, [partial_size, self.d - partial_size], dim=-1)

            norm_x = partial_x.norm(2, dim=-1, keepdim=True)
            d_x = partial_size

        rms_x = norm_x * d_x ** (-1. / 2)
        x_normed = x / (rms_x + self.eps)

        if self.bias:
            return self.scale * x_normed + self.offset

        return self.scale * x_normed

def symlogMSE(x, y):
    return F.mse_loss(symlog(x), symlog(y))

def logits_to_value(predicted_logits, num_bins=41, min_exp=1, max_exp=11):
    """Convert logits to actual values using exponential binning.
    
    Args:
        predicted_logits: Logits from the network (B, num_bins)
        num_bins: Number of bins to use
        min_exp: Minimum exponent for 2^x binning (default: 1, for 2^1 = 2)
        max_exp: Maximum exponent for 2^x binning (default: 11, for 2^11 = 2048)
    
    Returns:
        Predicted values computed as weighted sum of bin values
    """
    # Ensure input is 2D
    if predicted_logits.dim() == 1:
        predicted_logits = predicted_logits.unsqueeze(0)

    # Create exponentially spaced bins using powers of 2
    exponents = torch.linspace(min_exp, max_exp, num_bins).to(predicted_logits.device)
    bins = 2.0 ** exponents
    
    # Compute softmax probabilities
    softmax_probs = F.softmax(predicted_logits, dim=1)
    
    # Compute expected prediction (weighted sum)
    predicted_values = torch.sum(softmax_probs * bins, dim=1)
    
    return predicted_values

def twohot_exp_loss(predicted_logits, true_values, num_bins=41, min_exp=1, max_exp=11):
    """Compute two-hot encoded loss for exponentially spaced bins.
    
    Args:
        predicted_logits: Predicted distribution logits (B, num_bins)
        true_values: True values to encode (B,)
        num_bins: Number of bins to use
        min_exp: Minimum exponent for 2^x binning (default: 1, for 2^1 = 2)
        max_exp: Maximum exponent for 2^x binning (default: 11, for 2^11 = 2048)
    
    Returns:
        loss: Cross entropy loss between predicted and two-hot distribution
        predicted_values: The predicted values from the logits
    """
    # Ensure inputs are 2D
    if predicted_logits.dim() == 1:
        predicted_logits = predicted_logits.unsqueeze(0)
    if true_values.dim() == 0:
        true_values = true_values.unsqueeze(0)
    elif true_values.dim() == 1:
        true_values = true_values.unsqueeze(1)

    batch_size = true_values.shape[0]

    # Create exponentially spaced bins
    exponents = torch.linspace(min_exp, max_exp, num_bins).to(true_values.device)
    bins = 2.0 ** exponents
    
    # Find closest bins for true values
    # Convert to log2 space for easier comparison
    log2_true = torch.log2(true_values)
    k = torch.sum(exponents < log2_true, dim=1).long()
    k = torch.clamp(k, 0, num_bins - 2)
    
    # Get bin values
    lower_bin = bins[k]
    upper_bin = bins[k + 1]
    
    # Compute weights for twohot encoding
    weight_upper = (true_values.squeeze() - lower_bin) / (upper_bin - lower_bin)
    weight_lower = 1 - weight_upper
    
    # Create twohot encoding
    twohot = torch.zeros_like(predicted_logits)
    twohot.scatter_(1, k.unsqueeze(1), weight_lower.unsqueeze(1))
    twohot.scatter_(1, (k + 1).unsqueeze(1), weight_upper.unsqueeze(1))
    
    # Compute cross-entropy loss
    loss = F.cross_entropy(predicted_logits, twohot, reduction='mean')
    
    # Get predicted values
    predicted_values = logits_to_value(predicted_logits, num_bins, min_exp, max_exp)
    
    return loss, predicted_values


def plot_and_save(data, title, ylabel, filename, xlabel='Epoch'):
    plt.figure(figsize=(10, 6))
    plt.plot(range(1, len(data) + 1), data)
    plt.title(f'{title} over {xlabel}')
    plt.xlabel(xlabel)
    plt.ylabel(ylabel)
    plt.grid(True)
    plt.savefig(filename)
    plt.close()


def least_power_of_2(value):
    if value <= 0:
        return 1
    
    exponent = math.ceil(math.log(value, 2))
    return 2 ** exponent

class LastTokenSelector(nn.Module):
    def forward(self, x):
        return x[:, -1]

class AddUniformBase(nn.Module):
    def forward(self, x):
        return (0.99 * x) + (0.01*(1.0/x.shape[-1]))
    
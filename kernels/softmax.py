import triton,torch
import triton.language as tl 

def naive_softmax(x: torch.Tensor) -> torch.Tensor:
    """Compute row-wise softmax of X using native pytorch

    We subtract the maximum element in order to avoid overflows. Softmax is invariant to
    this shift.
    # in total: read 5MN + 2M elements ; wrote 3MN + 2M elements
    """
    x_max = x.max(dim=1)[0] # read  MN elements ; write M  elements
    safe_x = x - x_max[:, None] # read MN + M elements ; write MN elements
    numerator = torch.exp(safe_x) # read  MN elements ; write MN elements
    denominator = numerator.sum(dim=1) # read  MN elements ; write M  elements
    ret = numerator / denominator[:, None]  # read MN + M elements ; write MN elements
    
    return ret

@triton.jit
def _fwd_softmax_kernel(
    input_ptr,
    stride_input_row,
    output_ptr,
    stride_output_row,
    num_cols,
    BLOCK_SIZE: tl.constexpr
):
    # 1, setup input ptrs
    row_id = tl.program_id(axis=0)
    row_start_ptr = input_ptr + row_id * stride_input_row
    col_offsets = tl.arange(0, BLOCK_SIZE)
    input_pointers = row_start_ptr + col_offsets
    
    row_data_mask = col_offsets < num_cols
    
    # 2, move to SRAM
    x = tl.load(input_pointers, mask=row_data_mask, other=0.0)
    
    # 3, softmax cal itself
    safe_row = x - tl.max(x, axis=0)
    numerator = tl.exp(safe_row)
    denominator = tl.sum(numerator, axis=0)
    softmax_out = numerator / denominator
    
    # 4, write back to HBM
    output_row_ptr = output_ptr + row_id * stride_input_row
    output_pointers = output_row_ptr + col_offsets
    tl.store(output_pointers, softmax_out, mask = row_data_mask)

@torch.no_grad()
def softmax(x: torch.Tensor) -> torch.Tensor:
    """Triton impl of Softmax, onlay support 2D tensor in fwd"""
    rows, cols = x.shape
    assert x.ndim == 2, f"only accepts 2D tensor now"
    BLOCK_SIZE = triton.next_power_of_2(cols)
    num_warps = 4 # *32: threads
    if BLOCK_SIZE > 2047:
        num_warps = 8
    elif BLOCK_SIZE > 4095:
        num_warps = 16
        
    grid = (rows, 1)
    
    # allocate output buffer
    softmax_out = torch.empty_like(x)
    
    _fwd_softmax_kernel[grid](
        x,
        x.stride(0), # input row stride
        softmax_out,
        softmax_out.stride(0),
        cols,
        BLOCK_SIZE = BLOCK_SIZE,
        num_warps = num_warps,
    )
    
    return softmax_out
    
"""
pysteps.nowcasts.utils
======================

Module with common utilities used by nowcasts methods.

.. autosummary::
    :toctree: ../generated/

    print_ar_params
    print_corrcoefs
    stack_cascades
"""

import time
import numpy as np
from pysteps import extrapolation


def binned_timesteps(timesteps):
    """Compute a binning of the given irregular time steps.

    Parameters
    ----------
    timesteps: array_like
        List or one-dimensional array containing the time steps in ascending
        order.

    Returns
    -------
    out: list
        List of length int(np.ceil(timesteps[-1]))+1 containing the bins. Each
        element is a list containing the indices of the time steps falling in
        the bin (excluding the right edge).
    """
    timesteps = list(timesteps)
    if not sorted(timesteps) == timesteps:
        raise ValueError("timesteps is not in ascending order")

    if np.any(np.array(timesteps) < 0):
        raise ValueError("negative time steps are not allowed")

    num_bins = int(np.ceil(timesteps[-1]))
    timestep_range = np.arange(num_bins + 1)
    bin_idx = np.digitize(timesteps, timestep_range, right=False)

    out = [[] for i in range(num_bins + 1)]
    for i, bi in enumerate(bin_idx):
        out[bi - 1].append(i)

    return out


def compute_percentile_mask(precip, pct):
    """Compute a precipitation mask, where True/False values are assigned for
    pixels above/below the given percentile.

    Parameters
    ----------
    precip: array-like
        Two-dimensional array of shape (m,n) containing the input precipitation
        field.
    pct: float
        The percentile value.

    Returns
    -------
    out: ndarray_
        Array of shape (m,n), where True/False values are assigned for pixels
        above/below the precipitation intensity corresponding to the given
        percentile.
    """
    # obtain the CDF from the input precipitation field
    precip_s = precip.flatten()

    # compute the precipitation intensity threshold corresponding to the given
    # percentile
    precip_s.sort(kind="quicksort")
    x = 1.0 * np.arange(1, len(precip_s) + 1)[::-1] / len(precip_s)
    i = np.argmin(abs(x - pct))
    # handle ties
    if precip_s[i] == precip_s[i + 1]:
        i = np.where(precip_s == precip_s[i])[0][-1] + 1
    precip_pct_thr = precip_s[i]

    # determine the mask using the above threshold value
    return precip >= precip_pct_thr


def nowcast_main_loop(
    precip,
    velocity,
    state,
    timesteps,
    extrap_method,
    extrap_kwargs,
    func_state_update,
    func_decode,
    measure_time=False,
):
    """Utility method for advection-based nowcast models, where some parts of
    the model (e.g. an autoregressive process) require using integer time steps.

    Parameters
    ----------
    precip: array-like
        Array of shape (m,n) containing the most recently observed precipitation
        field.
    velocity: array-like
        Array of shape (2,m,n) containing the x- and y-components of the
        advection field.
    state : object
        The initial state of the nowcast model.
    timesteps: int or list of floats
        Number of time steps to forecast or a list of time steps for which the
        forecasts are computed. The elements of the list are required to be in
        ascending order.
    extrap_method: str, optional
        Name of the extrapolation method to use. See the documentation of
        :py:mod:`pysteps.extrapolation.interface`.
    extrap_kwargs: dict, optional
        Optional dictionary containing keyword arguments for the extrapolation
        method. See the documentation of pysteps.extrapolation.
    func_state_update : function
        A function that takes the current state of the nowcast model and returns
        the new state.
    func_decode : function
        A function that decoded the current state and returns a forecast field.
    measure_time: bool
        If set to True, measure, print and return the computation time.

    Returns
    -------
    out : list
        List of forecast fields for the given time steps. If measure_time is
        True, return a pair, where the second element is the total computation
        time in the loop.
    """
    precip_f = []

    # create a range of time steps
    # if an integer time step is given, create a simple range iterator
    # otherwise, assing the time steps to integer bins so that each bin
    # contains a list of time steps belonging to that bin
    if isinstance(timesteps, int):
        timesteps = range(timesteps + 1)
        timestep_type = "int"
    else:
        original_timesteps = [0] + list(timesteps)
        timesteps = binned_timesteps(original_timesteps)
        timestep_type = "list"

    state_prev = state
    precip_f_prev = precip
    displacement = None
    t_prev = 0.0

    # initialize the extrapolator
    extrapolator = extrapolation.get_method(extrap_method)

    x_values, y_values = np.meshgrid(
        np.arange(precip.shape[2]), np.arange(precip.shape[1])
    )

    xy_coords = np.stack([x_values, y_values])

    extrap_kwargs = extrap_kwargs.copy()
    extrap_kwargs["xy_coords"] = xy_coords
    extrap_kwargs["allow_nonfinite_values"] = True

    if measure_time:
        starttime_total = time.time()

    # loop through the integer time steps or bins if non-integer time steps
    # were given
    for t, subtimestep_idx in enumerate(timesteps):
        if timestep_type == "list":
            subtimesteps = [original_timesteps[t_] for t_ in subtimestep_idx]
        else:
            subtimesteps = [t]

        if (timestep_type == "list" and subtimesteps) or (
            timestep_type == "int" and t > 0
        ):
            is_nowcast_time_step = True
        else:
            is_nowcast_time_step = False

        # print a message if nowcasts are computed for the current integer time
        # step (this is not necessarily the case, since the current bin might
        # not contain any time steps)
        if is_nowcast_time_step:
            print(
                f"Computing nowcast for time step {t}... ",
                end="",
                flush=True,
            )

        # call the function to iterate the integer-part of the model for one
        # time step
        state_new = func_state_update(state_prev)
        precip_f_new = func_decode(state_new)

        if measure_time:
            starttime = time.time()

        # advect the currect forecast field to the subtimesteps in the current
        # bin and append the results to the output list
        # apply temporal interpolation to the forecasts made between the
        # previous and the next integer time steps
        for t_sub in subtimesteps:
            if t_sub > 0:
                t_diff_prev_int = t_sub - int(t_sub)
                if t_diff_prev_int > 0.0:
                    precip_f_ip = (
                        1.0 - t_diff_prev_int
                    ) * precip_f_prev + t_diff_prev_int * precip_f_new
                else:
                    precip_f_ip = precip_f_prev

                t_diff_prev = t_sub - t_prev
                extrap_kwargs["displacement_prev"] = displacement
                precip_f_ep, displacement = extrapolator(
                    precip_f_ip,
                    velocity,
                    [t_diff_prev],
                    **extrap_kwargs,
                )
                precip_f.append(precip_f_ep[0])
                t_prev = t_sub

        if not subtimesteps:
            t_diff_prev = t + 1 - t_prev
            extrap_kwargs["displacement_prev"] = displacement
            _, displacement = extrapolator(
                None,
                velocity,
                [t_diff_prev],
                **extrap_kwargs,
            )
            t_prev = t + 1

        precip_f_prev = precip_f_new
        state_prev = state_new

        if is_nowcast_time_step:
            if measure_time:
                print(f"{time.time() - starttime:.2f} seconds.")
            else:
                print("done.")

    if measure_time:
        return precip_f, time.time() - starttime_total
    else:
        return precip_f


def print_ar_params(PHI):
    """Print the parameters of an AR(p) model.

    Parameters
    ----------
    PHI: array_like
        Array of shape (n, p) containing the AR(p) parameters for n cascade
        levels.
    """
    print("****************************************")
    print("* AR(p) parameters for cascade levels: *")
    print("****************************************")

    n = PHI.shape[1]

    hline_str = "---------"
    for k in range(n):
        hline_str += "---------------"

    print(hline_str)
    title_str = "| Level |"
    for k in range(n - 1):
        title_str += "    Phi-%d     |" % (k + 1)
    title_str += "    Phi-0     |"
    print(title_str)
    print(hline_str)

    fmt_str = "| %-5d |"
    for k in range(n):
        fmt_str += " %-12.6f |"

    for k in range(PHI.shape[0]):
        print(fmt_str % ((k + 1,) + tuple(PHI[k, :])))
        print(hline_str)


def print_corrcoefs(GAMMA):
    """Print the parameters of an AR(p) model.

    Parameters
    ----------
    GAMMA: array_like
      Array of shape (m, n) containing n correlation coefficients for m cascade
      levels.
    """
    print("************************************************")
    print("* Correlation coefficients for cascade levels: *")
    print("************************************************")

    m = GAMMA.shape[0]
    n = GAMMA.shape[1]

    hline_str = "---------"
    for k in range(n):
        hline_str += "----------------"

    print(hline_str)
    title_str = "| Level |"
    for k in range(n):
        title_str += "     Lag-%d     |" % (k + 1)
    print(title_str)
    print(hline_str)

    fmt_str = "| %-5d |"
    for k in range(n):
        fmt_str += " %-13.6f |"

    for k in range(m):
        print(fmt_str % ((k + 1,) + tuple(GAMMA[k, :])))
        print(hline_str)


def stack_cascades(R_d, n_levels, convert_to_full_arrays=False):
    """Stack the given cascades into a larger array.

    Parameters
    ----------
    R_d: list
        List of cascades obtained by calling a method implemented in
        pysteps.cascade.decomposition.
    n_levels: int
        The number of cascade levels.

    Returns
    -------
    out: tuple
        A list of three-dimensional arrays containing the stacked cascade levels.
    """
    R_c = []

    n_inputs = len(R_d)

    for i in range(n_levels):
        R_ = []
        for j in range(n_inputs):
            R__ = R_d[j]["cascade_levels"][i]
            if R_d[j]["compact_output"] and convert_to_full_arrays:
                R_tmp = np.zeros(R_d[j]["weight_masks"].shape[1:], dtype=complex)
                R_tmp[R_d[j]["weight_masks"][i]] = R__
                R__ = R_tmp
            R_.append(R__)
        R_c.append(np.stack(R_))

    if not np.any([R_d[i]["compact_output"] for i in range(len(R_d))]):
        R_c = np.stack(R_c)

    return R_c

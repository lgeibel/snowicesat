from __future__ import absolute_import, division

from distutils.version import LooseVersion
from salem import Grid, wgs84
import os
import numpy as np
import numpy.ma as ma
import pyproj
import logging
import xarray as xr
from crampon import entity_task
from crampon import utils
import snowicesat.cfg as cfg
import snowicesat.utils as utils
from skimage import filters
from skimage import exposure
from skimage.io import imread
from scipy import stats
from scipy.optimize import leastsq, curve_fit
import matplotlib.pyplot as plt
import math
import pandas as pd

try:
    # rasterio V > 1.0
    from rasterio.merge import merge as merge_tool
except ImportError:
    from rasterio.tools.merge import merge as merge_tool
import rasterio
from rasterio.plot import show

# Module logger
log = logging.getLogger(__name__)


@entity_task(log)
def asmag_snow_mapping(gdir):
    """
    Performs Otsu_tresholding on sentinel-image
    of glacier and SLA retrieval as described by
     P. Rastner:
     "Automated mapping of snow cover on glaciers and
     calculation of snow line altitudes from multi-
     temporal Landsat data

     Stores snow cover map in asmag dimension of
     snow_cover.nc in variables snow_map and SLA
       :param gdirs: :py:class:`crampon.GlacierDirectory`
        A GlacierDirectory instance.
    :return: None
    """
    try:
        sentinel = xr.open_dataset(gdir.get_filepath('sentinel_temp'))
    except FileNotFoundError:
        print("Exiting asmag_snow_mapping")
        return

    # get NIR band as np array
    nir = sentinel.sel(band='B08', time=cfg.PARAMS['date'][0]).img_values.values / 10000
    if nir[nir > 0].size > 0:
        try:
            val = filters.threshold_otsu(nir[nir > 0])
        except ValueError:
            # All pixels cloud-covered and not detected correctly
            print("All pixels have same color")
            # Manually set as no snow
            val = 1
            bins_center = 0
            hist = 0

        hist, bins_center = exposure.histogram(nir[nir > 0])
    else:
        val = 1
        bins_center = 0
        hist = 0
        # no pixels are snow covered

    snow = nir > val
    snow = snow * 1

    # Get Snow Line Altitude :
    SLA = get_SLA_asmag(gdir, snow)
    if SLA is None:
        SLA = 0
    fig = plt.figure(figsize=(15, 10))
    plt.subplot(1, 3, 1)
    plt.plot(bins_center, hist, lw=2)
    plt.axvline(val, color='k', ls='--')
    plt.title('Histogram and Otsu-Treshold')

    plt.subplot(1, 3, 2)
    plt.imshow(nir, cmap='gray')
    b04 = sentinel.sel(band='B04', time=cfg.PARAMS['date'][0]).img_values.values / 10000
    b03 = sentinel.sel(band='B03', time=cfg.PARAMS['date'][0]).img_values.values / 10000
    b02 = sentinel.sel(band='B02', time=cfg.PARAMS['date'][0]).img_values.values / 10000

    rgb_image = np.array([b04, b03, b02]).transpose((1, 2, 0))

    plt.imshow(rgb_image)
    plt.title('RGB Image')
    plt.subplot(1, 3, 3)
    plt.imshow(nir, cmap='gray')
    plt.imshow(snow, alpha=0.5)
    plt.title('Snow Covered Area after Ostu-Tresholding')
    plt.suptitle(str(gdir.name + " - " + gdir.id), fontsize=18)
   # plt.show()
    plt.savefig(gdir.get_filepath('plt_otsu'), bbox_inches='tight')

    # write to netcdf:
    if not os.path.exists(gdir.get_filepath('snow_cover')):
        # create new dataset:
        snow_xr = sentinel.drop([band_id for band_id in sentinel['band'].values][:-1],
                                dim='band').squeeze('band', drop=True)
        snow_xr = snow_xr.drop(['img_values'])
        # add dimension: "Model" with entries: asmag, naegeli_orig, naegeli_improv
        snow_xr['model'] = ('model', ['asmag', 'naegeli_orig', 'naegeli_improv'])

        snow_xr['snow_map'] = (['model','time', 'y', 'x'],
                                 np.zeros((3,1,snow.shape[0], snow.shape[1]), dtype=np.uint16))
        # new variables "snow_map" and "SLA" (snow line altitude)
        snow_xr['SLA'] = (['model', 'time'], np.zeros((3, 1), dtype=np.uint16))

    else:
        snow_xr = xr.open_dataset(gdir.get_filepath('snow_cover'))

    #write variables into dataset:
    snow_xr['snow_map'].loc[dict(model='asmag', time=cfg.PARAMS['date'][0])] = snow
    snow_xr['SLA'].loc[dict(model='asmag', time=cfg.PARAMS['date'][0])] = SLA

    # safe to file
    snow_xr.to_netcdf(gdir.get_filepath('snow_cover'), 'w')

    sentinel.close()
    snow_xr.close()

def get_SLA_asmag(gdir, snow):
    """Snow line altitude retrieval as described in the ASMAG algorithm.
    Returns None if there is no 20m elevation band with over 50% snow cover
    :param: gdir: :py:class:`crampon.GlacierDirectory`
                    A GlacierDirectory instance.
            snow: binary snow cover map as np-Array
    :return: SLA in meters
    """
    # Get DEM:
    dem_ts = xr.open_dataset(gdir.get_filepath('dem_ts'))
    elevation_grid = dem_ts.isel(time=0, band=0).height_in_m.values
    # Convert DEM to 20 Meter elevation bands:
    cover = []
    for num, height in enumerate(range(int(elevation_grid[elevation_grid > 0].min()),
                                       int(elevation_grid.max()), 20)):
        if num > 0:
            # starting at second iteration:
            if snow.shape != elevation_grid.shape:
                if elevation_grid.shape[0] > snow.shape[0] or \
                        elevation_grid.shape[1] > snow.shape[1]:  # Shorten elevation grid
                    elevation_grid = elevation_grid[0:snow.shape[0], 0:snow.shape[1]]
                if elevation_grid.shape[0] < snow.shape[0]:  # Extend elevation grid: append row:
                    elevation_grid = np.append(elevation_grid,
                                               [elevation_grid[(elevation_grid.shape[0] -
                                                                snow.shape[0]), :]], axis=0)
                if elevation_grid.shape[1] < snow.shape[1]:  # append column
                    b = elevation_grid[:, (elevation_grid.shape[1] -
                                           snow.shape[1])].reshape(elevation_grid.shape[0], 1)
                    elevation_grid = np.hstack((elevation_grid, b))
                    # Expand grid on boundaries to obtain raster in same shape after

            # find all pixels with same elevation between "height" and "height-20":
            band_height = 20
            while band_height > 0:
                snow_band = snow[(elevation_grid > (height - band_height)) & (elevation_grid < height)]
                if snow_band.size == 0:
                    band_height -= 1
                else:
                    break
            # Snow cover on 20 m elevation band:
            if snow_band.size == 0:
                print("No snow cover")
                cover.append(0)
            else:
                cover.append(snow_band[snow_band == 1].size / snow_band.size)

    bands = 5
    num = 0
    if any(loc_cover > 0.5 for loc_cover in cover):
        while num < len(cover):
            # check if there are 5 continuous bands with snow cover > 50%
            if all(bins > 0.5 for bins in cover[num:(num + bands)]):
                # select lowest band as
                SLA = range(int(elevation_grid[elevation_grid > 0].min()),
                            int(elevation_grid.max()), 20)[num]
                print(SLA)
                break  # stop loop
            if num == (len(cover) - bands - 1):
                # if end of glacier is reached and no SLA found:
                bands = bands - 1
                # start search again
                num = 0
            num += 1
    else:
        return
    dem_ts.close()
    print(SLA)
    return SLA


@entity_task(log)
def naegeli_snow_mapping(gdir):
    """
    Performs snow cover mapping on sentinel-image
    of glacier as described in Naegeli, 2019- Change detection
     of bare-ice albedo in the Swiss Alps
    Creates snow cover map in naegeli_snow_cover variable in
    snow_cover.nc
       :param gdir: :py:class:`crampon.GlacierDirectory`
        A GlacierDirectory instance.
    :return:
    """
    try:
        sentinel = xr.open_dataset(gdir.get_filepath('sentinel_temp'))
    except FileNotFoundError:
        print("Exiting snow mapping 2", gdir)
        return
    print(gdir)
    if not sentinel.sel(band='B03', time=cfg.PARAMS['date'][0]). \
            img_values.values.any():  # check if all non-zero values in array
        print("Cloud cover too high for a good classification")
        return

    dem_ts = xr.open_dataset(gdir.get_filepath('dem_ts'))
    elevation_grid = dem_ts.isel(time=0, band=0).height_in_m.values

    # Albedo shortwave to broadband conversion after Knap:
    albedo_k = 0.726 * sentinel.sel(band='B03',
                                    time=cfg.PARAMS['date'][0]).img_values.values / 10000 \
               + 0.322 * (sentinel.sel(band='B03',
                                       time=cfg.PARAMS['date'][0]).img_values.values / 10000) ** 2 \
               + 0.015 * sentinel.sel(band='B08',
                                      time=cfg.PARAMS['date'][0]).img_values.values / 10000 \
               + 0.581 * (sentinel.sel(band='B08',
                                       time=cfg.PARAMS['date'][0]).img_values.values / 10000) ** 2

    # TODO: try with nir band only
    # #Albedo conversion after Liang
    # albedo_l = 0.356 * sentinel.sel(band='B02', time=cfg.PARAMS['date'][0]).img_values.values/10000 \
    #            + 0.130 * sentinel.sel(band='B04', time=cfg.PARAMS['date'][0]).img_values.values/10000 \
    #            + 0.373 * sentinel.sel(band='B08', time=cfg.PARAMS['date'][0]).img_values.values/10000 \
    #            + 0.085 * sentinel.sel(band='B11', time=cfg.PARAMS['date'][0]).img_values.values/10000 \
    #            + 0.072 * sentinel.sel(band='B12', time=cfg.PARAMS['date'][0]).img_values.values/10000 \
    #            + 0.0018
    # Limit Albedo to 1
    albedo_k[albedo_k > 1] = 1
    albedo = [albedo_k]
    plt.figure(figsize=(15, 10))
    plt.subplot(2, 2, 1)
    b04 = sentinel.sel(band='B04', time=cfg.PARAMS['date'][0]).img_values.values / 10000
    b03 = sentinel.sel(band='B03', time=cfg.PARAMS['date'][0]).img_values.values / 10000
    b02 = sentinel.sel(band='B02', time=cfg.PARAMS['date'][0]).img_values.values / 10000
    rgb_image = np.array([b04, b03, b02]).transpose((1, 2, 0))
    plt.imshow(albedo_k, cmap='gray')
    plt.imshow(rgb_image)
    plt.title("RGB Image")  # Peform primary suface type evaluation: albedo > 0.55 = snow,
    # albedo < 0.25 = ice, 0.25 < albedo < 0.55 = ambigous range,
    # Pixel-wise
    for albedo_ind in albedo:
        if albedo_ind.shape != elevation_grid.shape:
            if elevation_grid.shape[0] > albedo_ind.shape[0] or \
                    elevation_grid.shape[1] > albedo_ind.shape[1]:  # Shorten elevation grid
                elevation_grid = elevation_grid[0:albedo_ind.shape[0], 0:albedo_ind.shape[1]]
            if elevation_grid.shape[0] < albedo_ind.shape[0]:  # Extend elevation grid: append row:
                elevation_grid = np.append(elevation_grid,
                                           [elevation_grid[
                                            (elevation_grid.shape[0] -
                                             albedo_ind.shape[0]), :]], axis=0)
            if elevation_grid.shape[1] < albedo_ind.shape[1]:  # append column
                b = elevation_grid[:, (elevation_grid.shape[1] -
                                       albedo_ind.shape[1])]. \
                    reshape(elevation_grid.shape[0], 1)
                elevation_grid = np.hstack((elevation_grid, b))
                # Expand grid on boundaries to obtain raster in same shape after
        snow = albedo_ind > 0.55
        ambig = (albedo_ind < 0.55) & (albedo_ind > 0.2)
        plt.subplot(2, 2, 2)
        plt.imshow(albedo_ind)
        plt.imshow(snow * 2 + 1 * ambig, cmap="Blues_r")
        plt.contour(elevation_grid, cmap="hot",
                    levels=list(
                        range(int(elevation_grid[elevation_grid > 0].min()),
                              int(elevation_grid.max()),
                              int((elevation_grid.max() -
                                   elevation_grid[elevation_grid > 0].min()) / 10)
                              )))
        plt.colorbar()
        plt.title("Snow and Ambig. Area")

        # Find critical albedo: albedo at location with highest albedo slope
        # (assumed to be snow line altitude)

        # Albedo slope: get DEM and albedo of ambigous range, transform into vector
        if ambig.any():  # only use if ambigious area contains any True values
            dem_amb = elevation_grid[ambig]
            albedo_amb = albedo_ind[ambig]

            # Write dem and albedo into pandas DataFrame:
            df = pd.DataFrame({'dem_amb': dem_amb.tolist(),
                               'albedo_amb': albedo_amb.tolist()})
            # Sort values by elevation, drop negative values:
            df = df.sort_values(by=['dem_amb'])
            df = df[df.dem_amb > 0]

            # 2. find location with maximum albedo slope
            albedo_crit, SLA = max_albedo_slope_orig(df)

            # Result: both have very similar results, but fitting
            # function seems more stable --> will use this value

            # Derive corrected albedo with outlier suppression:
            albedo_corr = albedo_ind
            r_crit = 400

            for i in range(0, ambig.shape[0]):
                for j in range(0, ambig.shape[1]):
                    if ambig[i, j]:
                        albedo_corr[i, j] = albedo_ind[i, j] - \
                                            (SLA - elevation_grid[i, j]) * 0.005
                        # Secondary surface type evaluation on ambiguous range:
                        if albedo_corr[i, j] > albedo_crit:
                            snow[i, j] = True
                    # Probability test to eliminate extreme outliers:
                    if elevation_grid[i, j] < (SLA - r_crit):
                        snow[i, j] = False
                    if elevation_grid[i, j] > (SLA + r_crit):
                        snow[i, j] = True

            print(SLA)
        else:  # if no values in ambiguous area -->
            r_crit = 400
            # either all snow covered or no snow at all
            if snow[snow == 1].size / snow.size > 0.9:  # high snow cover:
                # Set SLA to lowest limit
                SLA = elevation_grid[elevation_grid > 0].min()
            elif snow[snow == 1].size / snow.size < 0.1:  # low/no snow cover:
                SLA = elevation_grid.max()

        plt.subplot(2, 2, 4)
        plt.imshow(albedo_k)
        plt.imshow(snow * 1, cmap="Blues_r")
        plt.contour(elevation_grid, cmap="hot",
                    levels=list(
                        range(int(elevation_grid[elevation_grid > 0].min()),
                              int(elevation_grid.max()),
                              int((elevation_grid.max() -
                                   elevation_grid[elevation_grid > 0].min()) / 10)
                              )))
        plt.colorbar()
        plt.contour(elevation_grid, cmap='Greens',
                    levels=[SLA - r_crit, SLA, SLA + r_crit])
        plt.title("Final snow mask Orig.")
        plt.suptitle(str(gdir.name + " - " + gdir.id), fontsize=18)
        #        plt.show()
        plt.savefig(gdir.get_filepath('plt_naegeli'), bbox_inches='tight')

    # Save snow cover map to .nc file:
    snow_xr = xr.open_dataset(gdir.get_filepath('snow_cover'))
    # write variables into dataset:
    snow_xr['snow_map'].loc[dict(model='naegeli_orig', time=cfg.PARAMS['date'][0])] = snow
    snow_xr['SLA'].loc[dict(model='naegeli_orig', time=cfg.PARAMS['date'][0])] = SLA
    # safe to file
    snow_xr.to_netcdf(gdir.get_filepath('snow_cover'), 'w')

    snow_xr.close()

    sentinel.close()


@entity_task(log)
def naegeli_improved_snow_mapping(gdir):
    """
    Performs snow cover mapping on sentinel-image
    of glacier as described in Naegeli, 2019- Change detection
     of bare-ice albedo in the Swiss Alps with an improved SLA mapping
     algorithm and variable r_crit
    Creates snow cover map in naegeli_snow_cover variable in
    snow_cover.nc
       :param gdir: :py:class:`crampon.GlacierDirectory`
        A GlacierDirectory instance.
    :return:
    """
    try:
        sentinel = xr.open_dataset(gdir.get_filepath('sentinel_temp'))
    except FileNotFoundError:
        print("Exiting snow mapping 2", gdir)
        return
    print(gdir)
    if not sentinel.sel(band='B03', time=cfg.PARAMS['date'][0]). \
            img_values.values.any():  # check if all non-zero values in array
        print("Cloud cover too high for a good classification")
        return

    dem_ts = xr.open_dataset(gdir.get_filepath('dem_ts'))
    elevation_grid = dem_ts.isel(time=0, band=0).height_in_m.values

    # Albedo shortwave to broadband conversion after Knap:
    albedo_k = 0.726 * sentinel.sel(band='B03',
                                    time=cfg.PARAMS['date'][0]).img_values.values / 10000 \
               + 0.322 * (sentinel.sel(band='B03',
                                       time=cfg.PARAMS['date'][0]).img_values.values / 10000) ** 2 \
               + 0.015 * sentinel.sel(band='B08',
                                      time=cfg.PARAMS['date'][0]).img_values.values / 10000 \
               + 0.581 * (sentinel.sel(band='B08',
                                       time=cfg.PARAMS['date'][0]).img_values.values / 10000) ** 2

    # TODO: try with nir band only
    # #Albedo conversion after Liang
    # albedo_l = 0.356 * sentinel.sel(band='B02', time=cfg.PARAMS['date'][0]).img_values.values/10000 \
    #            + 0.130 * sentinel.sel(band='B04', time=cfg.PARAMS['date'][0]).img_values.values/10000 \
    #            + 0.373 * sentinel.sel(band='B08', time=cfg.PARAMS['date'][0]).img_values.values/10000 \
    #            + 0.085 * sentinel.sel(band='B11', time=cfg.PARAMS['date'][0]).img_values.values/10000 \
    #            + 0.072 * sentinel.sel(band='B12', time=cfg.PARAMS['date'][0]).img_values.values/10000 \
    #            + 0.0018
    # Limit Albedo to 1
    albedo_k[albedo_k > 1] = 1
    albedo = [albedo_k]
    plt.figure(figsize=(15, 10))
    plt.subplot(2, 2, 1)
    b04 = sentinel.sel(band='B04', time=cfg.PARAMS['date'][0]).img_values.values / 10000
    b03 = sentinel.sel(band='B03', time=cfg.PARAMS['date'][0]).img_values.values / 10000
    b02 = sentinel.sel(band='B02', time=cfg.PARAMS['date'][0]).img_values.values / 10000
    rgb_image = np.array([b04, b03, b02]).transpose((1, 2, 0))
    plt.imshow(albedo_k, cmap='gray')
    plt.imshow(rgb_image)
    plt.title("RGB Image")  # Peform primary suface type evaluation: albedo > 0.55 = snow,
    # albedo < 0.25 = ice, 0.25 < albedo < 0.55 = ambigous range,
    # Pixel-wise
    for albedo_ind in albedo:
        if albedo_ind.shape != elevation_grid.shape:
            if elevation_grid.shape[0] > albedo_ind.shape[0] or \
                    elevation_grid.shape[1] > albedo_ind.shape[1]:  # Shorten elevation grid
                elevation_grid = elevation_grid[0:albedo_ind.shape[0], 0:albedo_ind.shape[1]]
            if elevation_grid.shape[0] < albedo_ind.shape[0]:  # Extend elevation grid: append row:
                elevation_grid = np.append(elevation_grid,
                                           [elevation_grid[
                                            (elevation_grid.shape[0] -
                                             albedo_ind.shape[0]), :]], axis=0)
            if elevation_grid.shape[1] < albedo_ind.shape[1]:  # append column
                b = elevation_grid[:, (elevation_grid.shape[1] -
                                       albedo_ind.shape[1])]. \
                    reshape(elevation_grid.shape[0], 1)
                elevation_grid = np.hstack((elevation_grid, b))
                # Expand grid on boundaries to obtain raster in same shape after
        snow = albedo_ind > 0.55
        ambig = (albedo_ind < 0.55) & (albedo_ind > 0.2)
        plt.subplot(2, 2, 2)
        plt.imshow(albedo_ind)
        plt.imshow(snow * 2 + 1 * ambig, cmap="Blues_r")
        plt.contour(elevation_grid, cmap="hot",
                    levels=list(
                        range(int(elevation_grid[elevation_grid > 0].min()),
                              int(elevation_grid.max()),
                              int((elevation_grid.max() -
                                   elevation_grid[elevation_grid > 0].min()) / 10)
                              )))
        plt.colorbar()
        plt.title("Snow and Ambig. Area")

        # Find critical albedo: albedo at location with highest albedo slope
        # (assumed to be snow line altitude)

        # Albedo slope: get DEM and albedo of ambigous range, transform into vector
        if ambig.any():  # only use if ambigious area contains any True values
            dem_amb = elevation_grid[ambig]
            albedo_amb = albedo_ind[ambig]

            # Write dem and albedo into pandas DataFrame:
            df = pd.DataFrame({'dem_amb': dem_amb.tolist(),
                               'albedo_amb': albedo_amb.tolist()})
            # Sort values by elevation, drop negative values:
            df = df.sort_values(by=['dem_amb'])

            # Try two ways to obatin critical albedo:
            # 1. Fitting to step function:
            # albedo_crit_fit, SLA_fit = max_albedo_slope_fit(df)

            # 2. Iterate over elevation bands with increasing resolution
            albedo_crit_it, SLA_it, r_square = max_albedo_slope_iterate(df)

            print(r_square)

            # Result: both have very similar results, but fitting
            # function seems more stable --> will use this value
            SLA = SLA_it
            albedo_crit = albedo_crit_it

            # Derive corrected albedo with outlier suppression:
            albedo_corr = albedo_ind
            r_crit = 400

            # Make r_crit dependant on r_squared value (how well
            # does a step function model fit the elevation-albedo-profile?

            # Maximum for r_crit: maximum of elevation distance between SLA
            # and either lowest or highest snow-covered pixel
            if snow[snow * 1 == 1].size > 0:
                r_crit_max = max(SLA - elevation_grid[snow * 1 == 1][
                    elevation_grid[snow * 1 == 1] > 0].min(),
                                 elevation_grid[snow * 1 == 1].max() - SLA)
                print("R_crit_max ", r_crit_max)
            else:
                r_crit_max = elevation_grid[elevation_grid > 0].max() - SLA
            r_crit_min = 0  # for perfect model fit
            r_crit = - r_square * r_crit_max + r_crit_max
            r_crit = min(r_crit_max, r_crit)
            print("R_crit ", r_crit)

            for i in range(0, ambig.shape[0]):
                for j in range(0, ambig.shape[1]):
                    if ambig[i, j]:
                        albedo_corr[i, j] = albedo_ind[i, j] - \
                                            (SLA - elevation_grid[i, j]) * 0.005
                        # Secondary surface type evaluation on ambiguous range:
                        if albedo_corr[i, j] > albedo_crit:
                            snow[i, j] = True
                    # Probability test to eliminate extreme outliers:
                    if elevation_grid[i, j] < (SLA - r_crit):
                        snow[i, j] = False
                    if elevation_grid[i, j] > (SLA + r_crit):
                        snow[i, j] = True

            print(SLA)
        else:  # if no values in ambiguous area -->
            r_crit = 400
            # either all now covered or no snow at all
            if snow[snow == 1].size / snow.size > 0.9:  # high snow cover:
                # Set SLA to lowest limit
                SLA = elevation_grid[elevation_grid > 0].min()
            elif snow[snow == 1].size / snow.size < 0.1:  # low/no snow cover:
                SLA = elevation_grid.max()

        plt.subplot(2, 2, 4)
        plt.imshow(albedo_k)
        plt.imshow(snow * 1, cmap="Blues_r")
        plt.contour(elevation_grid, cmap="hot",
                    levels=list(
                        range(int(elevation_grid[elevation_grid > 0].min()),
                              int(elevation_grid.max()),
                              int((elevation_grid.max() -
                                   elevation_grid[elevation_grid > 0].min()) / 10)
                              )))
        plt.colorbar()
        plt.contour(elevation_grid, cmap='Greens',
                    levels=[SLA - r_crit, SLA, SLA + r_crit])
        plt.title("Final snow mask")
        plt.suptitle(str(gdir.name + " - " + gdir.id), fontsize=18)
        #        plt.show()
        plt.savefig(gdir.get_filepath('plt_impr_naegeli'), bbox_inches='tight')

    # Save snow cover map to .nc file:

    sentinel.close()


def max_albedo_slope_iterate(df):
    """Finds elevation and value of highest
    albedo/elevation slope while iterating over elevation bins of
    decreasing height extend
    ---------
    Input: df: Dataframe  containing the variable dem_amb (elevations of ambiguous range)
    and albedo_amb (albedo values in ambiguous range)
    Return: alb_max_slope, max_loc: Albedo at maximum albedo slope and location
    of maximum albedo slope
            r_square: r_square value to determine the fit of a step function onto the
            elevation-albedo profile
    """

    # Smart minimum finding:
    # Iterate over decreasing elevation bands: (2 bands, 4 bands, 8 bands, etc.)
    # Sort into bands over entire range:
    df = df[df.dem_amb > 0]
    dem_min = int(round(df[df.dem_amb > 0].dem_amb.min()))
    dem_max = int(round(df.dem_amb.max()))
    alb_min = int(round(df[df.albedo_amb > 0].albedo_amb.min()))
    alb_max = int(round(df.albedo_amb.max()))
    delta_h = int(round((dem_max - dem_min) / 2))
    for i in range(0, int(np.log(df.dem_amb.size))):
        delta_h = int(round((dem_max - dem_min) / (2 ** (i + 1))))
        if delta_h > 25 and i > 0:  # only look at height bands with h > 20 Meters
            dem_avg = range(dem_min, dem_max, delta_h)
            albedo_avg = []
            # Sort array into height bands:
            for num, height_20 in enumerate(dem_avg):
                # Write index of df.dem that is between the
                # current and the next elevation band into list:
                albedo_in_band = df.albedo_amb[(df.dem_amb > height_20) &
                                               (df.dem_amb < height_20 + delta_h)].tolist()
                # Average over all albedo values in one band:
                if not albedo_in_band:  # if list is empty append 0
                    albedo_avg.append(0)
                else:  # if not append average albedo of elevation band
                    albedo_avg.append(sum(albedo_in_band) / len(albedo_in_band))
            for num, local_alb in enumerate(albedo_avg):
                if albedo_avg[num] is 0:  # Interpolate if value == 0 as
                    if num > 0:
                        if num == (len(albedo_avg) - 1):  # nearest neighbor:
                            albedo_avg[num] = albedo_avg[num - 1]
                        # interpolate between neighbours
                        else:
                            albedo_avg[num] = (albedo_avg[num - 1] +
                                               albedo_avg[num + 1]) / 2
                    else:
                        albedo_avg[num] = albedo_avg[num + 1]

            # Find elevation/location with steepest albedo slope
            # in the proximity of max values from
            # previous iteration:
            if i > 1:
                if max_loc > 0:
                    max_loc_sub = np.argmax((np.gradient
                                             (albedo_avg
                                              [(2 * max_loc - 2):(2 * max_loc + 2)])))
                    max_loc = 2 * max_loc - 2 + max_loc_sub
                    # new location of maximum albedo slope
                else:
                    max_loc = np.argmax((np.gradient(albedo_avg)))
                    # first definition of max. albedo slope
            else:
                max_loc = np.argmax((np.gradient(albedo_avg)))
            if max_loc < (len(albedo_avg) - 1):
                # find location between two values, set as final value for albedo
                # at SLA and SLA
                alb_max_slope = (albedo_avg[max_loc] + albedo_avg[max_loc + 1]) / 2
                height_max_slope = (dem_avg[max_loc] + dem_avg[max_loc + 1]) / 2
            else:
                # if SLA is at highest elevation, pick this valiue
                alb_max_slope = (albedo_avg[max_loc])
                height_max_slope = (dem_avg[max_loc])

    plt.subplot(2, 2, 3)
    try:
        plt.plot(dem_avg, albedo_avg)
    except UnboundLocalError:
        print("Glacier smaller than 25 meters")
        if delta_h == 0:
            delta_h = 1
        dem_avg = range(dem_min, dem_max, delta_h)
        albedo_avg = range(dem_min, dem_max, delta_h)
        # Take middle as a first guess:
        alb_max_slope = (alb_max - alb_min) / 2
        height_max_slope = (dem_max - dem_min) / 2
        plt.plot(dem_avg, albedo_avg)

    plt.axvline(height_max_slope, color='k', ls='--')
    plt.xlabel("Altitude in m")
    plt.ylabel("Albedo")

    # Fitting Step function to Determine fit with R^2:
    # curve fitting: bounds for inital model:
    # bounds:
    # a: step size of heaviside function: 0.1-0.3
    # b: elevation of snow - ice transition: dem_min - dem_max
    # c: average albedo of bare ice: 0.25-0.55

    popt, pcov = curve_fit(model, dem_avg, albedo_avg,
                           bounds=([0.1, dem_min, 0.3], [0.3, dem_max, 0.45]))

    residuals = abs(albedo_avg - model(dem_avg, popt[0], popt[1], popt[2]))
    ss_res = np.sum(residuals ** 2)
    ss_tot = np.sum((albedo_avg - np.mean(albedo_avg)) ** 2)
    r_squared = 1 - (ss_res / ss_tot)

    print("R squared = ", r_squared)

    return alb_max_slope, height_max_slope, r_squared


def max_albedo_slope_orig(df):
    """Finds elevation and value of highest
    albedo/elevation slope while iterating over elevation bins of
    decreasing height extend
    ---------
    Input: df: Dataframe  containing the variable dem_amb (elevations of ambiguous range)
    and albedo_amb (albedo values in ambiguous range)
    Return: alb_max_slope, max_loc: Albedo at maximum albedo slope and location
    of maximum albedo slope
            r_square: r_square value to determine the fit of a step function onto the
            elevation-albedo profile
    """

    # Smart minimum finding:
    # Iterate over decreasing elevation bands: (2 bands, 4 bands, 8 bands, etc.)
    # Sort into bands over entire range:
    df = df[df.dem_amb > 0]
    dem_min = int(round(df[df.dem_amb > 0].dem_amb.min()))
    dem_max = int(round(df.dem_amb.max()))
    alb_min = int(round(df[df.albedo_amb > 0].albedo_amb.min()))
    alb_max = int(round(df.albedo_amb.max()))
    delta_h = 20
    dem_avg = range(dem_min, dem_max, delta_h)
    albedo_avg = []
    # Sort array into height bands:
    for num, height_20 in enumerate(dem_avg):
        # Write index of df.dem that is between the
        # current and the next elevation band into list:
        albedo_in_band = df.albedo_amb[(df.dem_amb > height_20) &
                                       (df.dem_amb < height_20 + delta_h)].tolist()
        # Average over all albedo values in one band:
        if not albedo_in_band:  # if list is empty append 0
            albedo_avg.append(0)
        else:  # if not append average albedo of elevation band
            albedo_avg.append(sum(albedo_in_band) / len(albedo_in_band))
    for num, local_alb in enumerate(albedo_avg):
        if albedo_avg[num] is 0:  # Interpolate if value == 0 as
            if num > 0:
                if num == (len(albedo_avg) - 1):  # nearest neighbor:
                    albedo_avg[num] = albedo_avg[num - 1]
                    # interpolate between neighbours
                else:
                    albedo_avg[num] = (albedo_avg[num - 1] +
                                       albedo_avg[num + 1]) / 2
            else:
                albedo_avg[num] = albedo_avg[num + 1]

    # Find elevation/location with steepest albedo slope
    max_loc = np.argmax((np.gradient(albedo_avg)))
    if max_loc < (len(albedo_avg) - 1):
        # find location between two values, set as final value for albedo
        # at SLA and SLA
        alb_max_slope = (albedo_avg[max_loc] + albedo_avg[max_loc + 1]) / 2
        height_max_slope = (dem_avg[max_loc] + dem_avg[max_loc + 1]) / 2
    else:
        # if SLA is at highest elevation, pick this valiue
        alb_max_slope = (albedo_avg[max_loc])
        height_max_slope = (dem_avg[max_loc])

    plt.subplot(2, 2, 3)
    try:
        plt.plot(dem_avg, albedo_avg)
    except UnboundLocalError:
        print("Glacier smaller than 25 meters")
        if delta_h == 0:
            delta_h = 1
        dem_avg = range(dem_min, dem_max, delta_h)
        albedo_avg = range(dem_min, dem_max, delta_h)
        # Take middle as a first guess:
        alb_max_slope = (alb_max - alb_min) / 2
        height_max_slope = (dem_max - dem_min) / 2
        plt.plot(dem_avg, albedo_avg)

    plt.axvline(height_max_slope, color='k', ls='--')
    plt.xlabel("Altitude in m")
    plt.ylabel("Albedo")

    return alb_max_slope, height_max_slope


def max_albedo_slope_fit(df):
    """
    Finds albedo slope with fitting to step function
    :param df:  Dataframe  containing the variable dem_amb (elevations of ambiguous range)
    and albedo_amb (albedo values in ambiguous range)
    Returns: alb_max_slope, max_loc: Albedo at maximum albedo slope and location
    of maximum albedo slope
    """
    df = df[df.dem_amb > 0]
    dem_min = int(round(df[df.dem_amb > 0].dem_amb.min()))
    dem_max = int(round(df.dem_amb.max()))

    delta_h = int(round((dem_max - dem_min) / 30))
    delta_h = 1
    dem_avg = range(dem_min, dem_max, delta_h)
    albedo_avg = []  # Sort array into height bands:
    for num, height_20 in enumerate(dem_avg):
        # Write index of df.dem that is between the
        # current and the next elevation band into list:
        albedo_in_band = df.albedo_amb[(df.dem_amb > height_20) &
                                       (df.dem_amb < height_20 + 20)].tolist()
        # Average over all albedo values in one band:
        if not albedo_in_band:  # if list is empty append 0
            albedo_avg.append(0.25)
        else:  # if not append average albedo of elevation band
            albedo_avg.append(sum(albedo_in_band) / len(albedo_in_band))
    for num, local_alb in enumerate(albedo_avg):
        if albedo_avg[num] is 0:  # Interpolate if value == 0 as
            #  central difference (Boundaries cant be zero)
            albedo_avg[num] = (albedo_avg[num - 1] + albedo_avg[num + 1]) / 2

    # curve fitting: bounds for inital model:
    # bounds:
    # a: step size of heaviside function: 0.1-0.3
    # b: elevation of snow - ice transition: dem_min - dem_max
    # c: average albedo of bare ice: 0.25-0.55

    popt, pcov = curve_fit(model, dem_avg, albedo_avg,
                           bounds=([0.1, dem_min, 0.3], [0.3, dem_max, 0.45]))

    residuals = abs(albedo_avg - model(dem_avg, popt[0], popt[1], popt[2]))
    ss_res = np.sum(residuals ** 2)
    ss_tot = np.sum((albedo_avg - np.mean(albedo_avg)) ** 2)
    r_squared = 1 - (ss_res / ss_tot)

    print("R squared = ", r_squared)
    #    plt.subplot(2,3,3)
    #    plt.plot(dem_avg, albedo_avg, dem_avg, model(dem_avg, popt[0], popt[1], popt[2]))

    # get index of elevation of albedo- transition:
    max_loc = (np.abs(dem_avg - popt[1])).argmin()
    if max_loc < (len(albedo_avg) - 1):
        alb_max_slope = (albedo_avg[max_loc] + albedo_avg[max_loc + 1]) / 2
        height_max_slope = (dem_avg[max_loc] + dem_avg[max_loc + 1]) / 2
    else:
        alb_max_slope = albedo_avg[max_loc]
        height_max_slope = dem_avg[max_loc]
    return alb_max_slope, height_max_slope


def model(alti, a, b, c):
    """ Create model for step-function
    Input: alti: Altitude distribution of glacier
            a: step size of heaviside function
            b: elevation of snow-ice transition
            c: average albedo of bare ice
    Return: step-function model
    """
    return (0.5 * (np.sign(alti - b) + 1)) * a + c  # Heaviside fitting function

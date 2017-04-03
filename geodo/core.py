"""Some useful functions that did not fit into the other modules.

Copyright: OGGM developers, 2014-2015

License: GPLv3+
"""
from __future__ import absolute_import, division

import six.moves.cPickle as pickle
from six import string_types
from six.moves.urllib.request import urlretrieve, urlopen
from six.moves.urllib.error import HTTPError, URLError, ContentTooShortError

# Builtins
import glob
import os
import gzip
import shutil
import zipfile
import sys
import math
import logging
from collections import OrderedDict
from functools import partial, wraps
import json
import time
import fnmatch
import subprocess

# External libs
import geopandas as gpd
import pandas as pd
import salem
from salem import lazy_property, read_shapefile
import numpy as np
import netCDF4
from scipy import stats
from joblib import Memory
from shapely.ops import transform as shp_trafo
from salem import wgs84
import xarray as xr
import rasterio
try:
    from rasterio.tools.merge import merge as merge_tool
except ImportError:
    # rasterio V > 1.0
    from rasterio.merge import merge as merge_tool
import multiprocessing as mp
import filelock
import oggm.cfg as cfg
from oggm.cfg import CUMSEC_IN_MONTHS, SEC_IN_YEAR, BEGINSEC_IN_MONTHS
from oggm.utils import mkdir

SAMPLE_DATA_GH_REPO = 'OGGM/oggm-sample-data'
CRU_SERVER = 'https://crudata.uea.ac.uk/cru/data/hrg/cru_ts_3.24/cruts' \
             '.1609301803.v3.24/'


# Joblib
MEMORY = Memory(cachedir=cfg.CACHE_DIR, verbose=0)

# Function
tuple2int = partial(np.array, dtype=np.int64)

# Special regions for viewfinderpanoramas.org (Should be external!?)
DEM3REG = {
        'ISL': [-25., -12., 63., 67.],  # Iceland
        'SVALBARD': [10., 34., 76., 81.],
        'JANMAYEN': [-10., -7., 70., 72.],
        'FJ': [36., 66., 79., 82.],  # Franz Josef Land
        'FAR': [-8., -6., 61., 63.],  # Faroer
        'BEAR': [18., 20., 74., 75.],  # Bear Island
        'SHL': [-3., 0., 60., 61.],  # Shetland
        # Antarctica tiles as UTM zones, large files
        # '01-15': [-180., -91., -90, -60.],
        # '16-30': [-91., -1., -90., -60.],
        # '31-45': [-1., 89., -90., -60.],
        # '46-60': [89., 189., -90., -60.],
        # Greenland tiles
        # 'GL-North': [-78., -11., 75., 84.],
        # 'GL-West': [-68., -42., 64., 76.],
        # 'GL-South': [-52., -40., 59., 64.],
        # 'GL-East': [-42., -17., 64., 76.]
    }


def get_download_lock(lock_dir):
    mkdir(lock_dir)
    lockfile = os.path.join(lock_dir, 'download.lock')
    try:
        return filelock.FileLock(lockfile).acquire()
    except:
        return filelock.SoftFileLock(lockfile).acquire()


def _urlretrieve(url, ofile, *args, **kwargs):
    try:
        return urlretrieve(url, ofile, *args, **kwargs)
    except:
        if os.path.exists(ofile):
            os.remove(ofile)
        raise


def progress_urlretrieve(url, ofile):
    print("Downloading %s ..." % url)
    sys.stdout.flush()
    try:
        from progressbar import DataTransferBar, UnknownLength
        pbar = DataTransferBar()
        def _upd(count, size, total):
            if pbar.max_value is None:
                if total > 0:
                    pbar.start(total)
                else:
                    pbar.start(UnknownLength)
            pbar.update(min(count * size, total))
            sys.stdout.flush()
        res = _urlretrieve(url, ofile, reporthook=_upd)
        try:
            pbar.finish()
        except:
            pass
        return res
    except ImportError:
        return _urlretrieve(url, ofile)


def empty_cache(cdir):
    """
    Empty the cache directory.
    
    Parameters
    ----------
    cdir: str
        Path to the cache directory to empty
    """

    if os.path.exists(cdir):
        shutil.rmtree(cdir)
    os.makedirs(cdir)


def expand_path(p):
    """Helper function for os.path.expanduser and os.path.expandvars"""

    return os.path.expandvars(os.path.expanduser(p))


class SuperclassMeta(type):
    """Metaclass for abstract base classes.

    http://stackoverflow.com/questions/40508492/python-sphinx-inherit-
    method-documentation-from-superclass
    """
    def __new__(mcls, classname, bases, cls_dict):
        cls = super().__new__(mcls, classname, bases, cls_dict)
        for name, member in cls_dict.items():
            if not getattr(member, '__doc__'):
                try:
                    member.__doc__ = getattr(bases[-1], name).__doc__
                except AttributeError:
                    pass
        return cls


def download_gh_sample_files(repo, outdir):
    """
    Download sample files from a GitHub repository.
    
    Parameters
    ----------
    repo: str
        Name of the GitHub repository as string
    outdir: str, optional
        Path to the directory where to store the files
        
    Returns
    -------
    A dictionary indicating all downloaded files (keys) and their local paths
    (values).
    """
    with get_download_lock(lock_dir=outdir):
        return _download_gh_sample_files_unlocked(repo=repo, outdir=outdir)


def _download_gh_sample_files_unlocked(repo=None, outdir=None):
    """
    Checks for presence and downloads a GitHub data repo if needed.
    
    Parameters
    ----------
    repo : str
        Name of the GitHub repository as string
    outdir : str, optional
        Path to the cache directory

    Returns
    -------
    A dictionary indicating all downloaded files (keys) and their local paths
    (values).
    """

    if not repo:
        raise ValueError('No repository to download from specified.')

    if len(repo.split('/')) > 1:  # e.g. organization included
        repo_short = repo.split('/')[-1]
    else:
        repo_short = repo

    master_sha_url = 'https://api.github.com/repos/%s/commits/master' % \
                     repo
    master_zip_url = 'https://github.com/%s/archive/master.zip' % \
                     repo
    ofile = os.path.join(outdir, '{}.zip'.format(repo_short))
    shafile = os.path.join(outdir, '{}-commit.txt'.format(repo_short))
    odir = os.path.join(outdir)

    # a file containing the online's file's hash and the time of last check
    if os.path.exists(shafile):
        with open(shafile, 'r') as sfile:
            local_sha = sfile.read().strip()
        last_mod = os.path.getmtime(shafile)
    else:
        # very first download
        local_sha = '0000'
        last_mod = 0

    # test only every hour
    if time.time() - last_mod > 3600:
        write_sha = True
        try:
            # this might fail with HTTP 403 when server overload
            resp = urlopen(master_sha_url)

            # following try/finally is just for py2/3 compatibility
            # https://mail.python.org/pipermail/python-list/2016-March/704073.html
            try:
                json_str = resp.read().decode('utf-8')
            finally:
                resp.close()
            json_obj = json.loads(json_str)
            master_sha = json_obj['sha']
            # if not same, delete entire dir
            if local_sha != master_sha:
                empty_cache()
        except (HTTPError, URLError):
            master_sha = 'error'
    else:
        write_sha = False

    # download only if necessary
    if not os.path.exists(ofile):
        progress_urlretrieve(master_zip_url, ofile)

        # Trying to make the download more robust
        try:
            with zipfile.ZipFile(ofile) as zf:
                zf.extractall(odir)
        except zipfile.BadZipfile:
            # try another time
            if os.path.exists(ofile):
                os.remove(ofile)
            progress_urlretrieve(master_zip_url, ofile)
            with zipfile.ZipFile(ofile) as zf:
                zf.extractall(odir)

    # sha did change, replace
    if write_sha:
        with open(shafile, 'w') as sfile:
            sfile.write(master_sha)

    # list of files for output
    out = dict()
    sdir = os.path.join(outdir, '{}-master'.format(repo_short))
    for root, directories, filenames in os.walk(sdir):
        for filename in filenames:
            if filename in out:
                # This was a stupid thing, and should not happen
                # TODO: duplicates in sample data...
                k = os.path.join(os.path.dirname(root), filename)
                assert k not in out
                out[k] = os.path.join(root, filename)
            else:
                out[filename] = os.path.join(root, filename)

    return out


def download_srtm_file(zone, outdir):
    """
    Download an SRTM file of a specified zone.
    
    Parameters
    ----------
    zone: str
        A valid SRTM zone
    outdir: str
        Directory where to store the SRTM file 

    Returns
    -------
    Path to the downloaded SRTM file.
    """
    with get_download_lock(outdir):
        return _download_srtm_file_unlocked(zone, outdir)


def _download_srtm_file_unlocked(zone, outdir, retry=5):
    """Check if the srtm data is already in the directory. If not, download it.
    """

    mkdir(outdir)
    ofile = os.path.join(outdir, 'srtm_' + zone + '.zip')
#    ifile = 'http://srtm.csi.cgiar.org/SRT-ZIP/SRTM_V41/SRTM_Data_GeoTiff' \
    ifile = 'http://droppr.org/srtm/v4.1/6_5x5_TIFs' \
            '/srtm_' + zone + '.zip'
    if not os.path.exists(ofile):
        retry_counter = 0
        retry_max = retry
        while True:
            # Try to download
            try:
                retry_counter += 1
                progress_urlretrieve(ifile, ofile)
                with zipfile.ZipFile(ofile) as zf:
                    zf.extractall(outdir)
                break
            except HTTPError as err:
                # This works well for py3
                if err.code == 404:
                    # Ok so this *should* be an ocean tile
                    return None
                elif (500 <= err.code < 600) and retry_counter <= retry_max:
                    print("Downloading SRTM data failed with HTTP error %s, "
                          "retrying in 10 seconds... %s/%s" %
                          (err.code, retry_counter, retry_max))
                    time.sleep(10)
                    continue
                else:
                    raise
            except zipfile.BadZipfile:
                # This is for py2
                # Ok so this *should* be an ocean tile
                return None

    out = os.path.join(outdir, 'srtm_' + zone + '.tif')
    assert os.path.exists(out)
    return out


def download_dem3_viewpano(zone, outdir):
    """
    Download a viewfinderpanoramas.org file of a specified zone.
    
    Parameters
    ----------
    zone: str
        A valid zone from viewfinderpanoramas.org
    outdir: str
        The directory where to store the download

    Returns
    -------
    The path to the downloaded viewfinderpanoramas.org file
    """
    with get_download_lock(outdir):
        return _download_dem3_viewpano_unlocked(zone, outdir)


def _download_dem3_viewpano_unlocked(zone, outdir):
    """Checks if the srtm data is in the directory and if not, download it.
    """

    mkdir(outdir)
    ofile = os.path.join(outdir, 'dem3_' + zone + '.zip')
    outpath = os.path.join(outdir, zone+'.tif')

    # check if TIFF file exists already
    if os.path.exists(outpath):
        return outpath

    # some files have a newer version 'v2'
    if zone in ['R33', 'R34', 'R35', 'R36', 'R37', 'R38', 'Q32', 'Q33', 'Q34',
                'Q35', 'Q36', 'Q37', 'Q38', 'Q39', 'Q40', 'P31', 'P32', 'P33',
                'P34', 'P35', 'P36', 'P37', 'P38', 'P39', 'P40']:
        ifile = 'http://viewfinderpanoramas.org/dem3/' + zone + 'v2.zip'
    elif zone in ['01-15', '16-30', '31-45', '46-60']:
        ifile = 'http://viewfinderpanoramas.org/ANTDEM3/' + zone + '.zip'
    else:
        ifile = 'http://viewfinderpanoramas.org/dem3/' + zone + '.zip'

    if not os.path.exists(ofile):
        retry_counter = 0
        retry_max = 5
        while True:
            # Try to download
            try:
                retry_counter += 1
                progress_urlretrieve(ifile, ofile)
                with zipfile.ZipFile(ofile) as zf:
                    zf.extractall(outdir)
                break
            except HTTPError as err:
                # This works well for py3
                if err.code == 404:
                    # Ok so this *should* be an ocean tile
                    return None
                elif (500 <= err.code < 600) and retry_counter <= retry_max:
                    print("Downloading DEM3 data failed with HTTP error %s, "
                          "retrying in 10 seconds... %s/%s" %
                          (err.code, retry_counter, retry_max))
                    time.sleep(10)
                    continue
                else:
                    raise
            except ContentTooShortError:
                print("Downloading DEM3 data failed with ContentTooShortError"
                      " error %s, retrying in 10 seconds... %s/%s" %
                      (err.code, retry_counter, retry_max))
                time.sleep(10)
                continue

            except zipfile.BadZipfile:
                # This is for py2
                # Ok so this *should* be an ocean tile
                return None

    # Serious issue: sometimes, if a southern hemisphere URL is queried for
    # download and there is none, a NH zip file os downloaded.
    # Example: http://viewfinderpanoramas.org/dem3/SN29.zip yields N29!
    # BUT: There are southern hemisphere files that download properly. However,
    # the unzipped folder has the file name of
    # the northern hemisphere file. Some checks if correct file exists:
    if len(zone) == 4 and zone.startswith('S'):
        zonedir = os.path.join(outdir, zone[1:])
    else:
        zonedir = os.path.join(outdir, zone)
    globlist = glob.glob(os.path.join(zonedir, '*.hgt'))

    # take care of the special file naming cases
    if zone in DEM3REG.keys():
        globlist = glob.glob(os.path.join(outdir, '*', '*.hgt'))

    if not globlist:
        raise RuntimeError("We should have some files here, but we don't")

    # merge the single HGT files (can be a bit ineffective, because not every
    # single file might be exactly within extent...)
    rfiles = [rasterio.open(s) for s in globlist]
    dest, output_transform = merge_tool(rfiles)
    profile = rfiles[0].profile
    if 'affine' in profile:
        profile.pop('affine')
    profile['transform'] = output_transform
    profile['height'] = dest.shape[1]
    profile['width'] = dest.shape[2]
    profile['driver'] = 'GTiff'
    with rasterio.open(outpath, 'w', **profile) as dst:
        dst.write(dest)

    assert os.path.exists(outpath)
    # delete original files to spare disk space
    for s in globlist:
        os.remove(s)

    return outpath


def _download_aster_file(zone, unit):
    with get_download_lock():
        return _download_aster_file_unlocked(zone, unit)


def _download_aster_file_unlocked(zone, unit):
    """Checks if the aster data is in the directory and if not, download it.

    You need AWS cli and AWS credentials for this. Quoting Timo:

    $ aws configure

    Key ID und Secret you should have
    Region is eu-west-1 and Output Format is json.
    """

    odir = os.path.join(cfg.PATHS['topo_dir'], 'aster')
    mkdir(odir)
    fbname = 'ASTGTM2_' + zone + '.zip'
    dirbname = 'UNIT_' + unit
    ofile = os.path.join(odir, fbname)

    cmd = 'aws --region eu-west-1 s3 cp s3://astgtmv2/ASTGTM_V2/'
    cmd = cmd + dirbname + '/' + fbname + ' ' + ofile
    if not os.path.exists(ofile):
        subprocess.call(cmd, shell=True)
        if os.path.exists(ofile):
            # Ok so the tile is a valid one
            with zipfile.ZipFile(ofile) as zf:
                zf.extractall(odir)
        else:
            # Ok so this *should* be an ocean tile
            return None

    out = os.path.join(odir, 'ASTGTM2_' + zone + '_dem.tif')
    assert os.path.exists(out)
    return out


def _download_alternate_topo_file(fname):
    with get_download_lock():
        return _download_alternate_topo_file_unlocked(fname)


def _download_alternate_topo_file_unlocked(fname):
    """Checks if the special topo data is in the directory and if not,
    download it from AWS.

    You need AWS cli and AWS credentials for this. Quoting Timo:

    $ aws configure

    Key ID und Secret you should have
    Region is eu-west-1 and Output Format is json.
    """

    fzipname = fname + '.zip'
    # Here we had a file exists check

    odir = os.path.join(cfg.PATHS['topo_dir'], 'alternate')
    mkdir(odir)
    ofile = os.path.join(odir, fzipname)

    cmd = 'aws --region eu-west-1 s3 cp s3://astgtmv2/topo/'
    cmd = cmd + fzipname + ' ' + ofile
    if not os.path.exists(ofile):
        print('Downloading ' + fzipname + ' from AWS s3...')
        subprocess.call(cmd, shell=True)
        if os.path.exists(ofile):
            # Ok so the tile is a valid one
            with zipfile.ZipFile(ofile) as zf:
                zf.extractall(odir)
        else:
            # Ok so this *should* be an ocean tile
            return None

    out = os.path.join(odir, fname)
    assert os.path.exists(out)
    return out


def aws_file_download(aws_path, local_path, reset=False):
    with get_download_lock():
        return _aws_file_download_unlocked(aws_path, local_path, reset)


def _aws_file_download_unlocked(aws_path, local_path, reset=False):
    """Download a file from the AWS drive s3://astgtmv2/

    **Note:** you need AWS credentials for this to work.

    Parameters
    ----------
    aws_path: path relative to  s3://astgtmv2/
    local_path: where to copy the file
    reset: overwrite the local file
    """

    if reset and os.path.exists(local_path):
        os.remove(local_path)

    cmd = 'aws --region eu-west-1 s3 cp s3://astgtmv2/'
    cmd = cmd + aws_path + ' ' + local_path
    if not os.path.exists(local_path):
        subprocess.call(cmd, shell=True)
    if not os.path.exists(local_path):
        raise RuntimeError('Something went wrong with the download')


def srtm_zone(lon_ran, lat_ran):
    """
    Find the related SRTM zone(s) given latitude/longitude ranges.
    
    Parameters
    ----------
    lon_ran: array-like
        Longitude range
    lat_ran: array-like
        Latitude range

    Returns
    -------
    A list of SRTM zones coinciding with the given latitude/longitude range.
    """

    # SRTM are sorted in tiles of 5 degrees
    srtm_x0 = -180.
    srtm_y0 = 60.
    srtm_dx = 5.
    srtm_dy = -5.

    # quick n dirty solution to be sure that we will cover the whole range
    mi, ma = np.min(lon_ran), np.max(lon_ran)
    lon_ran = np.linspace(mi, ma, np.ceil((ma - mi) + 3))
    mi, ma = np.min(lat_ran), np.max(lat_ran)
    lat_ran = np.linspace(mi, ma, np.ceil((ma - mi) + 3))

    zones = []
    for lon in lon_ran:
        for lat in lat_ran:
            dx = lon - srtm_x0
            dy = lat - srtm_y0
            assert dy < 0
            zx = np.ceil(dx / srtm_dx)
            zy = np.ceil(dy / srtm_dy)
            zones.append('{:02.0f}_{:02.0f}'.format(zx, zy))
    return list(sorted(set(zones)))


def dem3_viewpano_zone(lon_ran, lat_ran, extra_reg=DEM3REG):
    """
    Returns a list of DEM3 zones on viewfinderpanoramas.org covering the 
    desired latitude/longitude range.

    For an overview of the region scheme, see:
    http://viewfinderpanoramas.org/Coverage%20map%20viewfinderpanoramas_org3.htm
    
    Parameters
    ----------
    lon_ran: array-like
        Longitude range
    lat_ran: array-like
        Latitude range
    extra_reg: dict
        A dictionary of the extra regions not following the scheme.

    Returns
    -------
    A list of viewfinderpanorama zones covering the latitude/longitude range.
    """

    for _f in extra_reg.keys():

        if (np.min(lon_ran) >= extra_reg[_f][0]) and \
           (np.max(lon_ran) <= extra_reg[_f][1]) and \
           (np.min(lat_ran) >= extra_reg[_f][2]) and \
           (np.max(lat_ran) <= extra_reg[_f][3]):

            # test some weird inset files in Antarctica
            if (np.min(lon_ran) >= -91.) and (np.max(lon_ran) <= -90.) and \
               (np.min(lat_ran) >= -72.) and (np.max(lat_ran) <= -68.):
                return ['SR15']

            elif (np.min(lon_ran) >= -47.) and (np.max(lon_ran) <= -43.) and \
                 (np.min(lat_ran) >= -61.) and (np.max(lat_ran) <= -60.):
                return ['SP23']

            elif (np.min(lon_ran) >= 162.) and (np.max(lon_ran) <= 165.) and \
                 (np.min(lat_ran) >= -68.) and (np.max(lat_ran) <= -66.):
                return ['SQ58']

            # test some Greenland tiles as GL-North is not rectangular
            elif (np.min(lon_ran) >= -66.) and (np.max(lon_ran) <= -60.) and \
                 (np.min(lat_ran) >= 80.) and (np.max(lat_ran) <= 83.):
                return ['U20']

            elif (np.min(lon_ran) >= -60.) and (np.max(lon_ran) <= -54.) and \
                 (np.min(lat_ran) >= 80.) and (np.max(lat_ran) <= 83.):
                return ['U21']

            elif (np.min(lon_ran) >= -54.) and (np.max(lon_ran) <= -48.) and \
                 (np.min(lat_ran) >= 80.) and (np.max(lat_ran) <= 83.):
                return ['U22']

            else:
                return [_f]

    # If the tile doesn't have a special name, its name can be found like this:
    # corrected SRTMs are sorted in tiles of 6 deg longitude and 4 deg latitude
    srtm_x0 = -180.
    srtm_y0 = 0.
    srtm_dx = 6.
    srtm_dy = 4.

    # quick n dirty solution to be sure that we will cover the whole range
    mi, ma = np.min(lon_ran), np.max(lon_ran)
    # TODO: Fabien, find out what Johannes wanted with this +3
    # +3 is just for the number to become still a bit larger
    lon_ex = np.linspace(mi, ma, np.ceil((ma - mi)/srtm_dy)+3)
    mi, ma = np.min(lat_ran), np.max(lat_ran)
    lat_ex = np.linspace(mi, ma, np.ceil((ma - mi)/srtm_dx)+3)

    zones = []
    for lon in lon_ex:
        for lat in lat_ex:
            dx = lon - srtm_x0
            dy = lat - srtm_y0
            zx = np.ceil(dx / srtm_dx)
            # convert number to letter
            zy = chr(int(abs(dy / srtm_dy)) + ord('A'))
            if lat >= 0:
                zones.append('%s%02.0f' % (zy, zx))
            else:
                zones.append('S%s%02.0f' % (zy, zx))
    return list(sorted(set(zones)))


def aster_zone(lon_ex, lat_ex):
    """Returns a list of ASTER V2 zones and units covering the desired extent.
    """

    # ASTER is a bit more work. The units are directories of 5 by 5,
    # tiles are 1 by 1. The letter in the filename depends on the sign
    units_dx = 5.

    # quick n dirty solution to be sure that we will cover the whole range
    mi, ma = np.min(lon_ex), np.max(lon_ex)
    lon_ex = np.linspace(mi, ma, np.ceil((ma - mi) + 3))
    mi, ma = np.min(lat_ex), np.max(lat_ex)
    lat_ex = np.linspace(mi, ma, np.ceil((ma - mi) + 3))

    zones = []
    units = []
    for lon in lon_ex:
        for lat in lat_ex:
            dx = np.floor(lon)
            zx = np.floor(lon / units_dx) * units_dx
            if math.copysign(1, dx) == -1:
                dx = -dx
                zx = -zx
                lon_let = 'W'
            else:
                lon_let = 'E'

            dy = np.floor(lat)
            zy = np.floor(lat / units_dx) * units_dx
            if math.copysign(1, dy) == -1:
                dy = -dy
                zy = -zy
                lat_let = 'S'
            else:
                lat_let = 'N'

            z = '{}{:02.0f}{}{:03.0f}'.format(lat_let, dy, lon_let, dx)
            u = '{}{:02.0f}{}{:03.0f}'.format(lat_let, zy, lon_let, zx)
            if z not in zones:
                zones.append(z)
                units.append(u)

    return zones, units


def get_demo_file(repo, fname, outdir):
    """Returns the path to the desired OGGM file."""

    d = download_gh_sample_files(repo, outdir)
    if fname in d:
        return d[fname]
    else:
        return None


def get_cru_cl_file():
    """Returns the path to the unpacked CRU CL file (is in sample data)."""

    download_gh_sample_files('OGGM/oggm-sample-data', cfg.CACHE_DIR)

    sdir = os.path.join(cfg.CACHE_DIR, 'oggm-sample-data-master', 'cru')
    fpath = os.path.join(sdir, 'cru_cl2.nc')
    if os.path.exists(fpath):
        return fpath
    else:
        with zipfile.ZipFile(fpath + '.zip') as zf:
            zf.extractall(sdir)
        assert os.path.exists(fpath)
        return fpath


def get_wgms_files():
    """Get the path to the default WGMS-RGI link file and the data dir.

    Returns
    -------
    (file, dir): paths to the files
    """

    if cfg.PATHS['wgms_rgi_links'] != '~':
        if not os.path.exists(cfg.PATHS['wgms_rgi_links']):
            raise ValueError('wrong wgms_rgi_links path provided.')
        # User provided data
        outf = cfg.PATHS['wgms_rgi_links']
        datadir = os.path.join(os.path.dirname(outf), 'mbdata')
        if not os.path.exists(datadir):
            raise ValueError('The WGMS data directory is missing')
        return outf, datadir

    # Roll our own
    download_gh_sample_files('OGGM/oggm-sample-data', cfg.CACHE_DIR)
    sdir = os.path.join(cfg.CACHE_DIR, 'oggm-sample-data-master', 'wgms')
    outf = os.path.join(sdir, 'rgi_wgms_links_2015_RGIV5.csv')
    assert os.path.exists(outf)
    datadir = os.path.join(sdir, 'mbdata')
    assert os.path.exists(datadir)
    return outf, datadir


def get_leclercq_files():
    """Get the path to the default Leclercq-RGI link file and the data dir.

    Returns
    -------
    (file, dir): paths to the files
    """

    if cfg.PATHS['leclercq_rgi_links'] != '~':
        if not os.path.exists(cfg.PATHS['leclercq_rgi_links']):
            raise ValueError('wrong leclercq_rgi_links path provided.')
        # User provided data
        outf = cfg.PATHS['leclercq_rgi_links']
        # TODO: This doesnt exist yet
        datadir = os.path.join(os.path.dirname(outf), 'lendata')
        # if not os.path.exists(datadir):
        #     raise ValueError('The Leclercq data directory is missing')
        return outf, datadir

    # Roll our own
    download_gh_sample_files('OGGM/oggm-sample-data', cfg.CACHE_DIR)
    sdir = os.path.join(cfg.CACHE_DIR, 'oggm-sample-data-master', 'leclercq')
    outf = os.path.join(sdir, 'rgi_leclercq_links_2012_RGIV5.csv')
    assert os.path.exists(outf)
    # TODO: This doesnt exist yet
    datadir = os.path.join(sdir, 'lendata')
    # assert os.path.exists(datadir)
    return outf, datadir


def get_glathida_file():
    """Get the path to the default WGMS-RGI link file and the data dir.

    Returns
    -------
    (file, dir): paths to the files
    """

    if cfg.PATHS['glathida_rgi_links'] != '~':
        if not os.path.exists(cfg.PATHS['glathida_rgi_links']):
            raise ValueError('wrong glathida_rgi_links path provided.')
        # User provided data
        return cfg.PATHS['glathida_rgi_links']

    # Roll our own
    download_gh_sample_files()
    sdir = os.path.join(cfg.CACHE_DIR, 'oggm-sample-data-master', 'glathida')
    outf = os.path.join(sdir, 'rgi_glathida_links_2014_RGIV5.csv')
    assert os.path.exists(outf)
    return outf


def get_rgi_dir():
    with get_download_lock():
        return _get_rgi_dir_unlocked()


def _get_rgi_dir_unlocked():
    """
    Returns a path to the RGI directory.

    If the files are not present, download them.

    Returns
    -------
    path to the RGI directory
    """

    # Be sure the user gave a sensible path to the rgi dir
    rgi_dir = cfg.PATHS['rgi_dir']
    if not os.path.exists(rgi_dir):
        raise ValueError('The RGI data directory does not exist!')

    bname = 'rgi50.zip'
    ofile = os.path.join(rgi_dir, bname)

    # if not there download it
    if not os.path.exists(ofile):  # pragma: no cover
        tf = 'http://www.glims.org/RGI/rgi50_files/' + bname
        progress_urlretrieve(tf, ofile)

        # Extract root
        with zipfile.ZipFile(ofile) as zf:
            zf.extractall(rgi_dir)

        # Extract subdirs
        pattern = '*_rgi50_*.zip'
        for root, dirs, files in os.walk(cfg.PATHS['rgi_dir']):
            for filename in fnmatch.filter(files, pattern):
                ofile = os.path.join(root, filename)
                with zipfile.ZipFile(ofile) as zf:
                    ex_root = ofile.replace('.zip', '')
                    mkdir(ex_root)
                    zf.extractall(ex_root)

    return rgi_dir


def get_cru_file(var=None):
    with get_download_lock():
        return _get_cru_file_unlocked(var)


def _get_cru_file_unlocked(var=None):
    """
    Returns a path to the desired CRU TS file.

    If the file is not present, download it.

    Parameters
    ----------
    var: 'tmp' or 'pre'

    Returns
    -------
    path to the CRU file
    """

    cru_dir = cfg.PATHS['cru_dir']

    # Be sure the user gave a sensible path to the climate dir
    if cru_dir == '~' or not os.path.exists(cru_dir):
        raise ValueError('The CRU data directory({}) does not exist!'.format(cru_dir))

    # Be sure input makes sense
    if var not in ['tmp', 'pre']:
        raise ValueError('CRU variable {} does not exist!'.format(var))

    # cru_ts3.23.1901.2014.tmp.dat.nc
    bname = 'cru_ts3.23.1901.2014.{}.dat.nc'.format(var)
    ofile = os.path.join(cru_dir, bname)

    # if not there download it
    if not os.path.exists(ofile):  # pragma: no cover
        tf = CRU_SERVER + '{}/cru_ts3.23.1901.2014.{}.dat.nc.gz'.format(var,
                                                                        var)
        progress_urlretrieve(tf, ofile + '.gz')
        with gzip.GzipFile(ofile + '.gz') as zf:
            with open(ofile, 'wb') as outfile:
                for line in zf:
                    outfile.write(line)

    return ofile


def get_topo_file(lon_ex, lat_ex, rgi_region=None, source=None):
    """
    Returns a path to the DEM file covering the desired extent.

    If the file is not present, download it. If the extent covers two or
    more files, merge them.

    Returns a downloaded SRTM file for [-60S;60N], and
    a corrected DEM3 from viewfinderpanoramas.org else

    Parameters
    ----------
    lon_ex : tuple, required
        a (min_lon, max_lon) tuple deliminating the requested area longitudes
    lat_ex : tuple, required
        a (min_lat, max_lat) tuple deliminating the requested area latitudes
    rgi_region : int, optional
        the RGI region number (required for the GIMP DEM)
    source : str or list of str, optional
        if you want to force the use of a certain DEM source. Available are:
          - 'USER' : file set in cfg.PATHS['dem_file']
          - 'SRTM' : SRTM v4.1
          - 'GIMP' : https://bpcrc.osu.edu/gdg/data/gimpdem
          - 'RAMP' : http://nsidc.org/data/docs/daac/nsidc0082_ramp_dem.gd.html
          - 'DEM3' : http://viewfinderpanoramas.org/
          - 'ASTER' : ASTER data
          - 'ETOPO1' : last resort, a very coarse global dataset

    Returns
    -------
    tuple: (path to the dem file, data source)
    """

    if source is not None and not isinstance(source, string_types):
        # check all user options
        for s in source:
            demf, source_str = get_topo_file(lon_ex, lat_ex,
                                             rgi_region=rgi_region,
                                             source=s)
            if os.path.isfile(demf):
                return demf, source_str

    # Did the user specify a specific DEM file?
    if 'dem_file' in cfg.PATHS and os.path.isfile(cfg.PATHS['dem_file']):
        source = 'USER' if source is None else source
        if source == 'USER':
            return cfg.PATHS['dem_file'], source

    # If not, do the job ourselves: download and merge stuffs
    topodir = cfg.PATHS['topo_dir']

    # GIMP is in polar stereographic, not easy to test if glacier is on the map
    # It would be possible with a salem grid but this is a bit more expensive
    # Instead, we are just asking RGI for the region
    if source == 'GIMP' or (rgi_region is not None and int(rgi_region) == 5):
        source = 'GIMP' if source is None else source
        if source == 'GIMP':
            gimp_file = _download_alternate_topo_file('gimpdem_90m.tif')
            return gimp_file, source

    # Same for Antarctica
    if source == 'RAMP' or (rgi_region is not None and int(rgi_region) == 19):
        if np.max(lat_ex) > -60:
            # special case for some distant islands
            source = 'DEM3' if source is None else source
        else:
            source = 'RAMP' if source is None else source
        if source == 'RAMP':
            gimp_file = _download_alternate_topo_file('AntarcticDEM_wgs84.tif')
            return gimp_file, source

    # Anywhere else on Earth we chack for DEM3, ASTER, or SRTM
    if (np.min(lat_ex) < -60.) or (np.max(lat_ex) > 60.) or \
                    source == 'DEM3' or source == 'ASTER':
        # default is DEM3
        source = 'DEM3' if source is None else source
        if source == 'DEM3':
            # use corrected viewpanoramas.org DEM
            zones = dem3_viewpano_zone(lon_ex, lat_ex)
            sources = []
            for z in zones:
                sources.append(download_dem3_viewpano(z))
            source_str = source
        if source == 'ASTER':
            # use ASTER
            zones, units = aster_zone(lon_ex, lat_ex)
            sources = []
            for z, u in zip(zones, units):
                sf = _download_aster_file(z, u)
                if sf is not None:
                    sources.append(sf)
            source_str = source
    else:
        source = 'SRTM' if source is None else source
        if source == 'SRTM':
            zones = srtm_zone(lon_ex, lat_ex)
            sources = []
            for z in zones:
                sources.append(download_srtm_file(z))
            source_str = source

    # For the very last cases a very coarse dataset ?
    if source == 'ETOPO1':
        t_file = os.path.join(topodir, 'ETOPO1_Ice_g_geotiff.tif')
        assert os.path.exists(t_file)
        return t_file, 'ETOPO1'

    # filter for None (e.g. oceans)
    sources = [s for s in sources if s is not None]

    if len(sources) < 1:
        raise RuntimeError('No topography file available!')

    if len(sources) == 1:
        return sources[0], source_str
    else:
        # merge
        zone_str = '+'.join(zones)
        bname = source_str.lower() + '_merged_' + zone_str + '.tif'

        if len(bname) > 200:  # file name way too long
            import hashlib
            hash_object = hashlib.md5(bname.encode())
            bname = hash_object.hexdigest() + '.tif'

        merged_file = os.path.join(topodir, source_str.lower(),
                                   bname)
        if not os.path.exists(merged_file):
            # check case where wrong zip file is downloaded from
            if all(x is None for x in sources):
                raise ValueError('Chosen lat/lon values are not available')
            # write it
            rfiles = [rasterio.open(s) for s in sources]
            dest, output_transform = merge_tool(rfiles)
            profile = rfiles[0].profile
            if 'affine' in profile:
                profile.pop('affine')
            profile['transform'] = output_transform
            profile['height'] = dest.shape[1]
            profile['width'] = dest.shape[2]
            profile['driver'] = 'GTiff'
            with rasterio.open(merged_file, 'w', **profile) as dst:
                dst.write(dest)
        return merged_file, source_str + '_MERGED'

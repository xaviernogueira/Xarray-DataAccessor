import logging
import warnings
import itertools
import pandas as pd
import numpy as np
import xarray as xr
from pathlib import Path
from datetime import datetime
from rasterio.enums import Resampling
from typing import (
    Dict,
    List,
    Optional,
    Tuple,
    Union,
)
from xarray_data_accessor.shared_types import (
    CoordsTuple,
    TableInput,
    ResampleDict,
    BoundingBoxDict,
)


def get_data_tables(
    variables: Optional[List[str]] = None,
    coords: Optional[Union[CoordsTuple, List[CoordsTuple]]] = None,
    csv_of_coords: Optional[TableInput] = None,
    coords_id_column: Optional[str] = None,
    xy_columns: Optional[Tuple[str, str]] = None,
    save_table_dir: Optional[Union[str, Path]] = None,
    save_table_suffix: Optional[str] = None,
    save_table_prefix: Optional[str] = None,
) -> Dict[str, Union[pd.DataFrame, Path]]:
    """
    Returns:
        A dictionary with variable names as keys, and dataframes as values
            if save_table_dir==None, or the output table path as values if
            save_table_dir is not None.
    """
    # init output dictionary
    out_dict = {}

    # clean variables input
    variables = _verify_variables(variables)

    # get x/y columns
    if xy_columns is None:
        xy_columns = ('lon', 'lat')
    x_col, y_col = xy_columns

    # get coords input from csv
    coords_df = _get_coords_df(
        coords=coords,
        csv_of_coords=csv_of_coords,
        coords_id_column=coords_id_column,
    )

    # get the point x/y values
    point_xs = coords_df[x_col].values
    point_ys = coords_df[y_col].values
    point_ids = [str(i) for i in coords_df.index.values]

    # get dimension names
    x_dim = xarray_dataset.attrs['x_dim']
    y_dim = xarray_dataset.attrs['y_dim']

    # get all coordinates from the dataset
    ds_xs = xarray_dataset[x_dim].values
    ds_ys = xarray_dataset[y_dim].values

    # get nearest lat/longs for each sample point
    nearest_x_idxs = np.abs(ds_xs - point_xs.reshape(-1, 1)).argmin(axis=1)
    nearest_y_idxs = np.abs(ds_ys - point_ys.reshape(-1, 1)).argmin(axis=1)

    # get a dict with point IDs as keys, and nearest x/y indices as values
    points_nearest_xy_idxs = dict(zip(
        point_ids,
        zip(nearest_x_idxs, nearest_y_idxs)
    ))

    # get all x/long to y/lat combos
    combos = list(itertools.product(
        range(len(xarray_dataset[x_dim].values)),
        range(len(xarray_dataset[y_dim].values)),
    ))

    # make sure they are in the right order to reshape!
    combo_dict = dict(zip(combos, range(len(combos))))

    # get point id to xy combo index
    id_to_index = {}
    for pid, coord in points_nearest_xy_idxs.items():
        id_to_index[pid] = combo_dict[coord]

    # clear some memory
    del (
        nearest_x_idxs,
        nearest_y_idxs,
        point_xs,
        point_ys,
        combos,
        combo_dict,
    )

    # prep chunks
    xarray_dataset = xarray_dataset.chunk(
        {'time': 10000, x_dim: 10, y_dim: 10}
    )

    # get data for each variable
    for variable in variables:
        out_dict[variable] = _get_data_table_vectorized(
            variable,
            point_ids,
            id_to_index,
            xy_dims=(x_dim, y_dim),
            save_table_dir=save_table_dir,
            save_table_suffix=save_table_suffix,
            save_table_prefix=save_table_prefix,
        )
    return out_dict


def delete_temp_files(
    xarray_dataset: Optional[xr.Dataset] = None,
) -> None:
    """If temp files were created, this deletes them"""

    temp_files = []
    for file in Path.cwd().iterdir():
        if 'temp_data' in file.name:
            temp_files.append(file)

    if len(temp_files) > 1 and xarray_dataset is not None:
        # try to unlink data from file
        xarray_dataset.close()

    could_not_delete = []
    for t_file in temp_files:
        try:
            t_file.unlink()
        except PermissionError:
            could_not_delete.append(t_file)
    if len(could_not_delete) > 0:
        warnings.warn(
            message=(
                f'Could not delete {len(could_not_delete)} temp files '
                f'in directory {Path.cwd()}. You may want to clean them manually.'
            ),
        )

    # Utility Functions ########################################################


def _resample_slice(
    data: xr.Dataset,
    resample_dict: ResampleDict,
) -> Tuple[int, xr.Dataset]:
    return (
        resample_dict['index'],
        data.rio.reproject(
            dst_crs=resample_dict['crs'],
            shape=(resample_dict['height'], resample_dict['width']),
            resampling=getattr(
                Resampling, resample_dict['resampling_method']),
            kwargs={'dst_nodata': np.nan},
        )
    )


def _coords_in_bbox(
    bbox: BoundingBoxDict,
    coords: CoordsTuple,
) -> bool:
    lat, lon = coords
    conditionals = [
        (lat <= bbox['north']),
        (lat >= bbox['south']),
        (lon <= bbox['east']),
        (lon >= bbox['west']),
    ]
    if len(list(set(conditionals))) == 1 and conditionals[0] is True:
        return True
    return False


def _verify_variables(
    xarray_dataset: xr.Dataset,
    variables: Optional[Union[str, List[str]]] = None,
) -> List[str]:
    if variables is None:
        return list(xarray_dataset.data_vars)
    elif isinstance(variables, str):
        variables = [variables]

    # check which variables are available
    cant_add_variables = []
    data_variables = []
    for v in variables:
        if v in list(xarray_dataset.data_vars):
            data_variables.append(v)
        else:
            cant_add_variables.append(v)
    variables = list(set(data_variables))
    if len(cant_add_variables) > 0:
        warnings.warn(
            f'The following requested variables are not in the dataset:'
            f' {cant_add_variables}.'
        )
    return data_variables


def _get_coords_df(
    csv_of_coords: Optional[TableInput] = None,
    coords: Optional[Union[CoordsTuple, List[CoordsTuple]]] = None,
    coords_id_column: Optional[str] = None,
) -> pd.DataFrame:
    if csv_of_coords is not None:
        if isinstance(csv_of_coords, str):
            csv_of_coords = Path(csv_of_coords)
        if isinstance(csv_of_coords, Path):
            if not csv_of_coords.exists() or not csv_of_coords.suffix == '.csv':
                raise ValueError(
                    f'param:csv_of_coords must be a valid .csv file.'
                )
        if isinstance(csv_of_coords, pd.DataFrame):
            coords_df = csv_of_coords
        else:
            coords_df = pd.read_csv(csv_of_coords)

        if coords_id_column is not None:
            coords_df.set_index(coords_id_column, inplace=True)

    elif coords is not None:
        # TODO: build a dataframe
        raise NotImplementedError
    else:
        raise ValueError(
            'Must specify either param:coords or param:csv_of_coords'
        )
    return coords_df


def _get_data_table_vectorized(
    xarray_dataset: xr.Dataset,
    variable: str,
    point_ids: List[str],
    id_to_index: Dict[str, int],
    xy_dims: Tuple[str, str],
    save_table_dir: Optional[Union[str, Path]] = None,
    save_table_suffix: Optional[str] = None,
    save_table_prefix: Optional[str] = None,
) -> pd.DataFrame:

    # unpack dimension names
    x_dim, y_dim = xy_dims
    logging.info(
        f'Extracting {variable} data (vectorized method)'
    )

    # get batches of max 100 points to avoid memory overflow
    batch_size = 100
    start_stops_idxs = list(range(
        0,
        len(xarray_dataset.time) + 1,
        batch_size,
    ))

    # init list to store dataframes
    out_dfs = []

    for i, num in enumerate(start_stops_idxs):
        start = num
        if num != start_stops_idxs[-1]:
            stop = start_stops_idxs[i + 1]
        else:
            stop = None
        logging.info(
            f'Processing time slice [{num}:{stop}]. datetime={datetime.now()}'
        )

        # make a copy of the data for our variable of interest
        ds = xarray_dataset[variable].isel(
            time=slice(start, stop)
        ).load()

        # convert x/y dimensions to integer indexes
        ds[x_dim] = list(range(len(ds[x_dim].values)))
        ds[y_dim] = list(range(len(ds[y_dim].values)))

        # "stack" the dataset and convert to a dataframe
        ds_df = ds.stack(
            xy_index=(x_dim, y_dim),
            create_index=False,
        ).to_dataframe().drop(columns=[x_dim, y_dim]).reset_index()
        del ds

        # pivot the dataframe to have all point combo ids as columns
        ds_df = ds_df.pivot(
            index='time',
            columns='xy_index',
            values=variable,
        )
        ds_df.index.name = 'datetime'

        # convert the dictionary to a dataframe
        index_map = pd.DataFrame(
            list(id_to_index.items()),
            columns=['key', 'index'],
        ).set_index('key')

        # get the point indexes to query data with
        point_indexes = index_map.loc[point_ids].values.flatten()
        data = ds_df.loc[:, point_indexes].values
        index = ds_df.index
        del ds_df

        # create your final dataframe
        out_dfs.append(
            pd.DataFrame(
                columns=point_ids,
                index=index,
                data=data,
            ).sort_index(axis=1).sort_index(axis=0)
        )
        del data
        del index
        del index_map

    out_df = pd.concat(
        out_dfs,
        axis=0,
    )
    del out_dfs

    # save to file
    if save_table_dir:
        logging.info(
            f'Saving df to {save_table_dir}, datetime={datetime.now()}'
        )
        table_path = _save_dataframe(
            out_df,
            variable=variable,
            save_table_dir=save_table_dir,
            save_table_suffix=save_table_suffix,
            save_table_prefix=save_table_prefix,
        )
        del out_df
        return table_path
    else:
        return out_df


def _save_dataframe(
    df: pd.DataFrame,
    variable: str,
    save_table_dir: Optional[Union[str, Path]] = None,
    save_table_suffix: Optional[str] = None,
    save_table_prefix: Optional[str] = None,
) -> Path:
    # save if necessary
    if not save_table_prefix:
        prefix = ''
    else:
        prefix = save_table_prefix

    no_success = False
    if isinstance(save_table_dir, str):
        save_table_dir = Path(save_table_dir)
    if not save_table_dir.exists():
        warnings.warn(
            f'Output directory {save_table_dir} does not exist!'
        )

    if save_table_suffix is None or save_table_suffix == '.parquet':
        out_path = Path(
            save_table_dir / f'{prefix}{variable}.parquet'
        )
        df.to_parquet(out_path)

    elif save_table_suffix == '.csv':
        out_path = Path(
            save_table_dir / f'{prefix}{variable}.csv'
        )
        df.to_csv(out_path)

    elif save_table_suffix == '.xlsx':
        out_path = Path(
            save_table_dir / f'{prefix}{variable}.xlsx'
        )
        df.to_excel(out_path)
    else:
        raise ValueError(
            f'{save_table_suffix} is not a valid table format!'
        )
    logging.info(
        f'Data for variable={variable} saved @ {save_table_dir}'
    )
    return out_path
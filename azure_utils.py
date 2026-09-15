"""Utilidades de Azure Blob Storage.

Las credenciales NO viven en el codigo: se leen del archivo .env (ver .env.example).
Variables soportadas, en orden de prioridad:

1. AZURE_STORAGE_CONNECTION_STRING
2. AZURE_STORAGE_ACCOUNT + AZURE_STORAGE_KEY
3. AZURE_STORAGE_ACCOUNT + AZURE_STORAGE_SAS_TOKEN
"""

import pandas as pd
import logging
from azure.storage.blob import BlobServiceClient, BlobClient, ContainerClient
import os
import zipfile
from io import BytesIO,StringIO
import re
import datetime
import json
import chardet
import gzip
import tempfile
from functools import lru_cache
from io import BytesIO

from dotenv import load_dotenv

load_dotenv()

ENDPOINT_SUFFIX = os.getenv("AZURE_STORAGE_ENDPOINT_SUFFIX", "core.windows.net")


class MissingAzureCredentials(RuntimeError):
    """Se lanza cuando el .env no tiene credenciales de Azure Blob Storage."""


def _account_name():
    account = os.getenv("AZURE_STORAGE_ACCOUNT")
    if account:
        return account
    conn = os.getenv("AZURE_STORAGE_CONNECTION_STRING", "")
    match = re.search(r"AccountName=([^;]+)", conn)
    return match.group(1) if match else None


# Los defaults del SDK (timeout de 20 s, bloques de 4 MB) se quedan cortos con los
# archivos de este datalake: rasters de ~60 MB, geojson de ~160 MB y csv de ~200 MB.
TRANSFER_OPTIONS = dict(
    connection_timeout=int(os.getenv("AZURE_CONNECTION_TIMEOUT", "600")),
    read_timeout=int(os.getenv("AZURE_READ_TIMEOUT", "600")),
    max_single_put_size=8 * 1024 * 1024,   # fuerza subida por bloques
    max_block_size=8 * 1024 * 1024,
    max_single_get_size=32 * 1024 * 1024,
    max_chunk_get_size=8 * 1024 * 1024,
    retry_total=int(os.getenv("AZURE_RETRY_TOTAL", "6")),
    retry_connect=3,
    retry_read=3,
)


@lru_cache(maxsize=None)
def _build_client(conn_str, account_url, sas_token):
    if conn_str:
        return BlobServiceClient.from_connection_string(conn_str, **TRANSFER_OPTIONS)
    return BlobServiceClient(account_url=account_url, credential=sas_token, **TRANSFER_OPTIONS)


def get_blob_service_client_sas(account_url=None, credential=None):
    """Devuelve un BlobServiceClient usando las credenciales del .env.

    Los parametros se mantienen por compatibilidad con el codigo existente:
    `credential` solo se usa si se pasa explicitamente (connection string, SAS
    token o account key); si viene vacio manda el .env.
    """
    conn_str = os.getenv("AZURE_STORAGE_CONNECTION_STRING")
    account = _account_name()
    key = os.getenv("AZURE_STORAGE_KEY")
    sas = os.getenv("AZURE_STORAGE_SAS_TOKEN")

    if credential:
        if credential.startswith("DefaultEndpointsProtocol"):
            conn_str = credential
        elif credential.startswith("?") or credential.startswith("sv="):
            sas, conn_str = credential, None
        else:
            key, conn_str = credential, None

    if not conn_str:
        if account and key:
            conn_str = (
                f"DefaultEndpointsProtocol=https;AccountName={account};"
                f"AccountKey={key};EndpointSuffix={ENDPOINT_SUFFIX}"
            )
        elif account and sas:
            url = account_url or f"https://{account}.blob.{ENDPOINT_SUFFIX}/"
            return _build_client(None, url, sas if sas.startswith("?") else f"?{sas}")
        else:
            raise MissingAzureCredentials(
                "Faltan credenciales de Azure. Copia .env.example a .env y define "
                "AZURE_STORAGE_CONNECTION_STRING (o AZURE_STORAGE_ACCOUNT + "
                "AZURE_STORAGE_KEY / AZURE_STORAGE_SAS_TOKEN)."
            )

    return _build_client(conn_str, None, None)

def write_blob_file(blob_path, container_name, local_file_path):

    try:
        blob_service_client = get_blob_service_client_sas()
        container_client = blob_service_client.get_container_client(container_name)
        blob_client = container_client.get_blob_client(blob_path)

        with open(local_file_path, "rb") as data:
            blob_client.upload_blob(data, overwrite=True, max_concurrency=4)

    except Exception as e:
        logging.error(f"Error upbloading file to Blob Storage: {e}")
        raise



def save_group_to_parquet_temp(group, df, container_name, blob_path):
    with tempfile.TemporaryDirectory() as temp_dir:
        temp_path = f"{temp_dir}/{group}.parquet"
        df.to_parquet(temp_path, index=False, compression='snappy')
        upload_blob_in_chunks(temp_path, container_name, f'{blob_path}/{group}.parquet')

def upload_blob_in_chunks(file_path, container_name, blob_name):
    blob_service_client = get_blob_service_client_sas() 
    blob_client = blob_service_client.get_blob_client(container=container_name, blob=blob_name)
    
    with open(file_path, "rb") as data:
        blob_client.upload_blob(data, blob_type="BlockBlob", overwrite=True, max_concurrency=4)


def blob_exists(container_name, blob_path):
    blob_service_client =  get_blob_service_client_sas() 
    container_client = blob_service_client.get_container_client(container_name)
    blob_list = container_client.list_blobs(name_starts_with=blob_path)
    
    for blob in blob_list:
        if blob.name == blob_path:
            return True
    return False

def list_blob_files(container_name, blob_path):
    blob_service_client =  get_blob_service_client_sas() 
    container_client = blob_service_client.get_container_client(container_name)
    blob_list = container_client.list_blobs(name_starts_with=blob_path)
    return [blob.name for blob in blob_list]

def create_blob_directory(container_name, parent_path, directory_name):
    blob_service_client =  get_blob_service_client_sas() 
    container_client = blob_service_client.get_container_client(container_name)
    

    blob_name = f"{parent_path}/{directory_name}/.dummy"
    blob_client = container_client.get_blob_client(blob_name)
    
    blob_client.upload_blob(b"", overwrite=True)
    print(f"Directory '{directory_name}' created under '{parent_path}' in container '{container_name}'.")


def read_parquet_blob(path, container_name):
    blob_service_client = get_blob_service_client_sas()
    blob_client = blob_service_client.get_blob_client(container=container_name, blob=path)
    stream_downloader = blob_client.download_blob()
    stream = BytesIO(stream_downloader.readall())
    return pd.read_parquet(stream, engine='pyarrow')

def read_blob_df(path, container_name):
    blob_service_client = get_blob_service_client_sas()  

    container_client = blob_service_client.get_container_client(container_name)
    parquet_blobs = [blob.name for blob in container_client.list_blobs(name_starts_with=path) if blob.name.endswith(".parquet")]

    dataframes = [read_parquet_blob(blob_name, container_name) for blob_name in parquet_blobs]

    return pd.concat(dataframes, ignore_index=True) if dataframes else pd.DataFrame()


def read_sql_from_blob(path,container_name,credential=''):
    """
    Read the content of an SQL file from Azure Blob Storage.

    """
    blob_service_client = get_blob_service_client_sas()
    blob_client = blob_service_client.get_blob_client(container=container_name, blob=path)
    blob_data = blob_client.download_blob()
    return blob_data.content_as_text()


def read_blob_df_csv(path, container_name, credential='', encoding='utf-8'):
    blob_service_client = get_blob_service_client_sas()
    container_client = blob_service_client.get_container_client(container_name)
    blobs = container_client.list_blobs(name_starts_with=path)
    csv_blobs = [blob.name for blob in blobs if blob.name.endswith(".csv")]
    full_df = []
    for blob_name in csv_blobs:
        blob_client = blob_service_client.get_blob_client(container=container_name, blob=blob_name)
        stream_downloader = blob_client.download_blob()
        blob_content = stream_downloader.content_as_bytes()
        detected_encoding = chardet.detect(blob_content)['encoding'] if encoding is None else encoding
        df = pd.read_csv(StringIO(blob_content.decode(detected_encoding, errors='replace')), encoding=detected_encoding, low_memory=False)
        full_df.append(df)
        #print(blob_name, detected_encoding)
    full_df = pd.concat(full_df)
    return full_df


def read_blob_df_json(path, container_name):
    blob_service_client = get_blob_service_client_sas()
    container_client = blob_service_client.get_container_client(container_name)
    blobs = container_client.list_blobs(name_starts_with=path)
    json_blobs = [blob.name for blob in blobs if blob.name.endswith(".json")]
    full_df = []
    for blob_name in json_blobs:
        blob_client = blob_service_client.get_blob_client(container=container_name, blob=blob_name)
        stream_downloader = blob_client.download_blob()
        aux = json.loads(stream_downloader.readall())
        df = pd.DataFrame(aux)
        full_df.append(df)
    full_df = pd.concat(full_df)
    return full_df

def read_blob_df_geojson(path, container_name):
    import geopandas as gpd  # import perezoso: solo el paso geo necesita geopandas

    blob_service_client = get_blob_service_client_sas()
    container_client = blob_service_client.get_container_client(container_name)
    blobs = container_client.list_blobs(name_starts_with=path)
    geojson_blobs = [blob.name for blob in blobs if blob.name.endswith(".geojson")]
    full_gdf = []
    for blob_name in geojson_blobs:
        blob_client = blob_service_client.get_blob_client(container=container_name, blob=blob_name)
        stream_downloader = blob_client.download_blob()
        stream = BytesIO(stream_downloader.readall())
        gdf = gpd.read_file(stream)
        full_gdf.append(gdf)
    if full_gdf:
        # pd.concat funciona perfectamente con GeoDataFrames, ignore_index evita duplicidad de índices
        final_gdf = pd.concat(full_gdf, ignore_index=True)
        return final_gdf

def write_blob_parquet(path,container_name,df,credential=''):
    blob_service_client = get_blob_service_client_sas()
    blob_client = blob_service_client.get_blob_client(container=container_name, blob=path)
    parquet_file = BytesIO()
    df.to_parquet(parquet_file, engine='pyarrow')
    parquet_file.seek(0)  
    blob_client.upload_blob(data=parquet_file,overwrite=True)
    return True

def write_blob_df(path, container_name, df, credential=''):
    try:
        blob_service_client = get_blob_service_client_sas()
       
        if df.empty:
            print("The DataFrame is empty. No file will be uploaded.")
            return False
        
        blob_client = blob_service_client.get_blob_client(container=container_name, blob=path)

        parquet_file = BytesIO()
        df.to_parquet(parquet_file, engine='pyarrow')
        parquet_file.seek(0)  

        blob_client.upload_blob(data=parquet_file, overwrite=True, max_concurrency=4, timeout=300)
        
        parquet_file.close() 
        return True
    
    except Exception as e:
        print(f"Error subiendo archivo a Blob Storage: {e}")
        return False

def delete_blob(container_name, blob_name, credential=''):
    blob_service_client = get_blob_service_client_sas()

    blob_client = blob_service_client.get_blob_client(container=container_name, blob=blob_name)
    
    try:
        blob_client.delete_blob()
        print(f"Blob {blob_name} eliminado del contenedor {container_name}.")
    except Exception as e:
        print(f"Error al eliminar el blob: {e}")


def write_blob_df_csv(path, container_name, df, credential=''):
    blob_service_client = get_blob_service_client_sas()
    blob_client = blob_service_client.get_blob_client(container=container_name, blob=path)
    # Usamos utf-8-sig para que Excel y otras herramientas reconozcan tildes y eñes
    csv_content = df.to_csv(index=False, encoding='utf-8-sig')
    # Convertimos el string a bytes para la subida
    data_bytes = csv_content.encode('utf-8-sig')
    blob_client.upload_blob(data=data_bytes, overwrite=True, max_concurrency=4)
    return True


def write_blob_df_excel(path, container_name, df, credential=''):
    blob_service_client = get_blob_service_client_sas()
    blob_client = blob_service_client.get_blob_client(container=container_name, blob=path)
    # Crear un objeto BytesIO para almacenar el archivo Excel en memoria
    excel_file = BytesIO()
    # Usar ExcelWriter para escribir el DataFrame en un archivo Excel
    with pd.ExcelWriter(excel_file, engine='xlsxwriter') as writer:
        df.to_excel(writer, index=False)
    # Colocar el puntero al inicio del archivo en memoria
    excel_file.seek(0)
    # Subir el archivo Excel a Azure Blob Storage
    blob_client.upload_blob(data=excel_file, overwrite=True, timeout=6000)
    return True

def write_dict_blob_json(path, container_name, dict, credential=''):
    # Conectar al cliente de Azure Blob Storage
    blob_service_client = get_blob_service_client_sas(credential=credential)
    blob_client = blob_service_client.get_blob_client(container=container_name, blob=path)

    json_data = json.dumps(dict, ensure_ascii=False, indent=4)
    
    json_bytes = BytesIO(json_data.encode('utf-8'))
    
    # Subir el archivo JSON al Blob Storage
    blob_client.upload_blob(data=json_bytes, overwrite=True)

    return True

def get_last_updated_file_in_folder(path,container_name="ko-processed-data",credential=''):
    blob_service_client=get_blob_service_client_sas()
    container_client = blob_service_client.get_container_client(container_name)
    blobs = container_client.list_blobs(name_starts_with=path)
    blob_names = [blob.name[blob.name.rfind("/") + 1:] for blob in blobs]
    if len(blob_names)>0:
        last_created_file=max(blob_names)
        if path[-1] == '/':
             path=path
        else: 
              path = path[:path.rfind("/")]+"/"
        ret=os.path.splitext(last_created_file)[0]
    else:
        path=""
        last_created_file=""
        ret=""
    path=path+last_created_file
    return path,ret


def create_zip_from_df_array(dfs,output_path,container_name,credential=''):
    blob_service_client=get_blob_service_client_sas()
    with BytesIO() as mem_zip:
        with zipfile.ZipFile(mem_zip, mode="w", compression=zipfile.ZIP_DEFLATED) as zip_file:
            for file_name, df in dfs.items():             
                 zip_file.writestr(file_name, df.to_csv(index=False))
    
        mem_zip.seek(0)
        blob_client = blob_service_client.get_blob_client(container=container_name, blob=output_path)
        blob_client.upload_blob(mem_zip.getvalue(),overwrite=True)
    return output_path

def bottler_id_translation(bottler_id):
    id=''
    if bottler_id=="276f4223-630d-476d-bd02-3940354fda5c":
        bottler_id='276f4223-630d-476d-bd02-3940354fda5c'
        id="276f4223-630d-476d-bd02-3940354fda5c"
    return bottler_id,id


def list_files_in_folder(path,container_name="ko-processed-data",credential=''):
    blob_service_client=get_blob_service_client_sas()
    container_client = blob_service_client.get_container_client(container_name)
    blobs = container_client.list_blobs(name_starts_with=path)
    blob_names = [blob.name[blob.name.rfind("/") + 1:] for blob in blobs]
    blobs = container_client.list_blobs(name_starts_with=path)
    blob_path = [blob.name for blob in blobs]
    created_at=[datetime.datetime.fromtimestamp(int(re.search(r'\d{10}', names).group())).strftime('%Y-%m-%d %H:%M:%S') for names in blob_names]
    df_list = pd.DataFrame({'created_at': created_at, 'file_name': blob_names, 'path': blob_path})
    return df_list


def get_directory_list(path,container_name="ko-processed-data",credential=''):
    blob_service_client=get_blob_service_client_sas()
    container_client = blob_service_client.get_container_client(container_name)
    blobs = container_client.list_blobs(name_starts_with=path)
    blobs = sorted(blobs, key=lambda x: x.last_modified, reverse=True)
    blob_names = [blob.name for blob in blobs]
    return blob_names


def get_directory_list_by_date(path, container_name="ko-processed-data", credential=''):
    blob_service_client=get_blob_service_client_sas()
    container_client = blob_service_client.get_container_client(container_name)
    blobs = container_client.list_blobs(name_starts_with=path)
    blobs = sorted(blobs, key=lambda x: x.last_modified, reverse=True)
    blob_names = [(blob.name, blob.last_modified) for blob in blobs]
    return blob_names


def update_status_db(bottler_id,status,container_name='ko-ontrade-data'):
    """
    This function updates the table with the status
    1 : running,
    -1 : failed
    0 : finished

    its located in status/bottler_id/status.csv
    """
    from datetime import datetime
    path = "status/{}/status.csv".format(bottler_id)
    df = pd.DataFrame({'status': [status]})
    date = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    df['date'] = str(date)
    df['bottler_id'] = bottler_id
    df['file_id'] = " "
    if status==-1:
        df['error'] = 1
    else:
        df['error'] = 0

    status_df = read_blob_df_csv(path,container_name)
    status_df = pd.concat([status_df, df])
    write_blob_df_csv(path=path,df=status_df,container_name=container_name)
    return True

    
def add_request_count(bottler_id, count_type, count, container = "ko-ontrade-data"):
    try:
        from datetime import datetime
        path_to_count_df = "requests/request_count.csv".format(bottler_id)
        count_df = read_blob_df_csv(path_to_count_df,container)
        df = pd.DataFrame()
        df['count_type'] = [count_type]
        df['count'] = [count]
        df['bottler_id'] = [bottler_id]
        df['date'] = [datetime.now().strftime("%Y-%m-%d")]
        count_df = pd.concat([count_df,df])
        write_blob_df_csv(path_to_count_df,container,count_df)
    except Exception as e:
        print(e)
        return False
    return True



def read_blob_df_csv_optimized(path, container_name, credential=''):
    blob_service_client = get_blob_service_client_sas()
    container_client = blob_service_client.get_container_client(container_name)
    blobs = container_client.list_blobs(name_starts_with=path)
    csv_blobs = [blob.name for blob in blobs if blob.name.endswith(".csv")]

    dtype_spec = {
        'request_code': 'int64',
        'found_by': 'str',
        'icon': 'str',
        'icon_background_color': 'str',
        'icon_mask_base_uri': 'str',
        'name': 'str',
        'photos': 'str',
        'place_id': 'str',
        'reference': 'str',
        'scope': 'str',
        'types': 'str',
        'vicinity': 'str',
        'geometry.location.lat': 'float64',
        'geometry.location.lng': 'float64',
        'geometry.viewport.northeast.lat': 'float64',
        'geometry.viewport.northeast.lng': 'float64',
        'geometry.viewport.southwest.lat': 'float64',
        'geometry.viewport.southwest.lng': 'float64',
        'business_status': 'str',
        'rating': 'float64',
        'user_ratings_total': 'float64',
        'plus_code.compound_code': 'str',
        'plus_code.global_code': 'str',
        'opening_hours.open_now': 'str',
        'permanently_closed': 'str'
    }

    def read_csv_from_blob(blob_name):
        blob_client = blob_service_client.get_blob_client(container=container_name, blob=blob_name)
        stream_downloader = blob_client.download_blob()
        stream = BytesIO(stream_downloader.readall())
        print("read")
        return pd.read_csv(stream, encoding='latin-1', dtype=dtype_spec)


    full_df = pd.concat((read_csv_from_blob(blob_name) for blob_name in csv_blobs), ignore_index=True)

    return full_df

def read_excel_blob(path, container_name):
    blob_service_client = get_blob_service_client_sas()
    blob_client = blob_service_client.get_blob_client(container=container_name, blob=path)
    stream_downloader = blob_client.download_blob()
    stream = BytesIO(stream_downloader.readall())
    return pd.read_excel(stream)

def read_json_to_dict(path,container_name,credential=''):
    blob_service_client = get_blob_service_client_sas()
    container_client = blob_service_client.get_container_client(container_name)
    blobs = container_client.list_blobs(name_starts_with=path)
    json_blobs = [blob.name for blob in blobs if blob.name.lower().endswith(".json")]
    full_dict = {}
    for blob_name in json_blobs:
        blob_client = blob_service_client.get_blob_client(container=container_name, blob=blob_name)
        stream_downloader = blob_client.download_blob()
        dict = json.loads(stream_downloader.content_as_text())
        full_dict.update(dict)
    return full_dict

def read_blob_df_single_gzcsv(file_name, container_name, columns=None, chunk_size=100000,dtype_dict=None):
    blob_service_client = get_blob_service_client_sas()
    container_client = blob_service_client.get_container_client(container_name)

    blob_client = blob_service_client.get_blob_client(container=container_name, blob=file_name)
    stream_downloader = blob_client.download_blob().readall()

    chunk_list = []

    with gzip.GzipFile(fileobj=BytesIO(stream_downloader)) as f:
        for chunk in pd.read_csv(StringIO(f.read().decode('utf-8')), usecols=columns, chunksize=chunk_size, dtype=dtype_dict):
            chunk_list.append(chunk)

    df = pd.concat(chunk_list, ignore_index=True)

    return df


def load_data(path, container_name):
    try:
        df = None  
        if path.endswith('.xlsx'):
            df = read_excel_blob(path, container_name)
        elif path.endswith('.csv'):
            df = read_blob_df_csv(path, container_name)
        elif path.endswith('.parquet'):
            df = read_blob_df(path, container_name)
        elif path.endswith('.json'):
            df = read_json_to_dict(path, container_name)
        elif path.endswith('.gz'):
            df = read_blob_df_single_gzcsv(path, container_name)
        else:
            print(f"Error: No se reconoce el tipo de archivo para la ruta: {path}")
            return None
        
        if df is None:
            print(f"Error: No se pudo cargar el archivo en {path}.")
            return None

    except Exception as e:
        print(f"Error loading data: {e}")
        return None
    
    return df

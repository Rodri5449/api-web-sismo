import boto3
import uuid
import os
import re
import json
import urllib.request
from datetime import datetime
from decimal import Decimal

# La página https://ultimosismo.igp.gob.pe/productos/reportes-sismicos se arma
# con JavaScript: el HTML no trae la tabla. Los datos vienen de este API JSON,
# que es el mismo que usa la página para llenar la tabla.
URL_API = "https://ultimosismo.igp.gob.pe/api/ultimo-sismo/ajaxb/{anio}"
CANTIDAD = 10


def log(tipo, log_datos):
    print(json.dumps({'tipo': tipo, 'log_datos': log_datos}, default=str))


def obtener_sismos(anio):
    req = urllib.request.Request(
        URL_API.format(anio=anio),
        headers={'User-Agent': 'Mozilla/5.0', 'Accept': 'application/json'}
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        datos = json.loads(resp.read().decode('utf-8'))
    # El API puede devolver la lista directamente o dentro de una clave
    if isinstance(datos, dict):
        datos = datos.get('data') or datos.get('sismos') or []
    return datos


def clave_fecha(sismo):
    # Ordena por fecha_local + hora_local (acepta "2026-11-08", "08/11/2026",
    # "2026-11-08T00:00:00.000Z", "14:23:05", "1970-01-01T14:23:05"...)
    fecha = str(sismo.get('fecha_local', ''))
    hora = str(sismo.get('hora_local', ''))
    m = re.search(r'(\d{4})-(\d{2})-(\d{2})', fecha)
    if m:
        y, mes, d = m.groups()
    else:
        m = re.search(r'(\d{2})/(\d{2})/(\d{4})', fecha)
        d, mes, y = m.groups() if m else ('0', '0', '0')
    if 'T' in hora:
        hora = hora.split('T')[1]
    h = re.search(r'(\d{1,2}):(\d{2})(?::(\d{2}))?', hora)
    hh, mm, ss = (h.group(1), h.group(2), h.group(3) or '0') if h else ('0', '0', '0')
    return (int(y), int(mes), int(d), int(hh), int(mm), int(ss), str(sismo.get('codigo', '')))


def lambda_handler(event, context):
    try:
        nombre_tabla = os.environ['TABLE_NAME']

        # 1. Descargar sismos del año actual (y del anterior si hay menos de 10)
        anio = datetime.utcnow().year
        sismos = obtener_sismos(anio)
        if len(sismos) < CANTIDAD:
            sismos += obtener_sismos(anio - 1)

        if not sismos:
            log("ERROR", {'mensaje': 'El API del IGP no devolvió sismos', 'anio': anio})
            return {'statusCode': 404, 'body': 'No se encontraron sismos en el IGP'}

        # 2. Ordenar del más reciente al más antiguo y tomar los 10 últimos
        sismos.sort(key=clave_fecha, reverse=True)
        ultimos = sismos[:CANTIDAD]

        dynamodb = boto3.resource('dynamodb')
        table = dynamodb.Table(nombre_tabla)

        # 3. Eliminar los elementos anteriores de la tabla (con paginación)
        scan = table.scan(ProjectionExpression='id')
        items = scan['Items']
        while 'LastEvaluatedKey' in scan:
            scan = table.scan(ProjectionExpression='id',
                              ExclusiveStartKey=scan['LastEvaluatedKey'])
            items += scan['Items']
        with table.batch_writer() as batch:
            for each in items:
                batch.delete_item(Key={'id': each['id']})

        # 4. Insertar los 10 sismos
        rows = []
        with table.batch_writer() as batch:
            for i, sismo in enumerate(ultimos, start=1):
                row = dict(sismo)
                row['#'] = i
                row['id'] = str(uuid.uuid4())
                rows.append(row)
                # DynamoDB no acepta float: se convierten a Decimal
                batch.put_item(Item=json.loads(json.dumps(row, default=str),
                                               parse_float=Decimal))

        log("INFO", {'mensaje': 'Sismos guardados', 'tabla': nombre_tabla,
                     'cantidad': len(rows), 'sismos': rows})
        return {
            'statusCode': 200,
            'body': rows
        }

    except Exception as e:
        log("ERROR", {'mensaje': str(e), 'error_tipo': type(e).__name__})
        return {
            'statusCode': 500,
            'body': 'Error al obtener los sismos: ' + str(e)
        }

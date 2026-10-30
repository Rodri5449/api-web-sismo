import json
import re
import unicodedata
from datetime import datetime, timezone
from decimal import Decimal
from urllib.parse import urljoin

import boto3
import requests
from bs4 import BeautifulSoup

URL_IGP = "https://ultimosismo.igp.gob.pe/productos/reportes-sismicos"
NOMBRE_TABLA = "TablaSismosIGP"


def normalizar_texto(texto):
    texto = unicodedata.normalize("NFKD", texto or "")
    texto = "".join(caracter for caracter in texto if not unicodedata.combining(caracter))
    texto = re.sub(r"\s+", " ", texto)
    return texto.strip().lower()


def limpiar_valor(texto):
    return re.sub(r"\s+", " ", texto or "").strip()


def crear_registro(codigo, referencia, semaforo, magnitud, fecha, url_reporte=""):
    codigo = limpiar_valor(codigo)
    referencia = limpiar_valor(referencia)
    semaforo = limpiar_valor(semaforo).lower()
    fecha = limpiar_valor(fecha)

    if not codigo or not referencia or not magnitud or not fecha:
        return None

    try:
        magnitud_decimal = Decimal(magnitud.replace(",", "."))
    except Exception:
        return None

    return {
        "id": codigo,
        "codigo_reporte": codigo,
        "referencia": referencia,
        "semaforo": semaforo,
        "magnitud": magnitud_decimal,
        "fecha_hora_origen_local": fecha,
        "url_reporte": url_reporte or URL_IGP
    }


def extraer_desde_tabla(soup):
    for tabla_html in soup.find_all("table"):
        filas = tabla_html.find_all("tr")
        if not filas:
            continue

        encabezados = [normalizar_texto(celda.get_text(" ", strip=True)) for celda in filas[0].find_all(["th", "td"])]
        if not encabezados:
            continue

        indices = {}
        for indice, encabezado in enumerate(encabezados):
            if "cod reporte" in encabezado or "codigo reporte" in encabezado:
                indices["codigo"] = indice
            elif "referencia" in encabezado:
                indices["referencia"] = indice
            elif "semaforo" in encabezado:
                indices["semaforo"] = indice
            elif "magnitud" in encabezado:
                indices["magnitud"] = indice
            elif "fecha y hora de origen local" in encabezado:
                indices["fecha"] = indice

        if len(indices) != 5:
            continue

        registros = []
        for fila in filas[1:]:
            celdas = fila.find_all("td")
            if not celdas or max(indices.values()) >= len(celdas):
                continue

            valores = [celda.get_text(" ", strip=True) for celda in celdas]
            enlace = fila.find("a", href=True)
            url_reporte = urljoin(URL_IGP, enlace["href"]) if enlace else URL_IGP
            registro = crear_registro(
                valores[indices["codigo"]],
                valores[indices["referencia"]],
                valores[indices["semaforo"]],
                valores[indices["magnitud"]],
                valores[indices["fecha"]],
                url_reporte
            )
            if registro:
                registros.append(registro)

        if registros:
            return registros[:10]

    return []


def extraer_desde_texto(soup):
    texto = soup.get_text(" ", strip=True)
    patron = re.compile(
        r"(?P<codigo>IGP/CENSIS/RS\s+\d{4}-\d+)"
        r".*?Referencia\s*:?\s*(?P<referencia>.*?)"
        r"\s+Sem[aá]foro\s*:?\s*(?P<semaforo>.*?)"
        r"\s+Magnitud\s*:?\s*(?P<magnitud>\d+(?:[.,]\d+)?)"
        r"\s+Fecha y hora de origen local\s*:?\s*(?P<fecha>\d{2}/\d{2}/\d{4}\s+\d{2}:\d{2}:\d{2})",
        re.IGNORECASE | re.DOTALL
    )

    registros = []
    for coincidencia in patron.finditer(texto):
        registro = crear_registro(
            coincidencia.group("codigo"),
            coincidencia.group("referencia"),
            coincidencia.group("semaforo"),
            coincidencia.group("magnitud"),
            coincidencia.group("fecha")
        )
        if registro:
            registros.append(registro)

    return registros[:10]


def extraer_ultimos_sismos(html):
    soup = BeautifulSoup(html, "html.parser")
    registros = extraer_desde_tabla(soup)

    if not registros:
        registros = extraer_desde_texto(soup)

    unicos = []
    codigos = set()
    for registro in registros:
        if registro["id"] not in codigos:
            codigos.add(registro["id"])
            unicos.append(registro)
        if len(unicos) == 10:
            break

    return unicos


def guardar_en_dynamodb(registros):
    tabla = boto3.resource("dynamodb").Table(NOMBRE_TABLA)
    ids_actuales = {registro["id"] for registro in registros}
    ids_existentes = set()

    respuesta = tabla.scan(ProjectionExpression="#identificador", ExpressionAttributeNames={"#identificador": "id"})
    while True:
        ids_existentes.update(item["id"] for item in respuesta.get("Items", []))
        clave = respuesta.get("LastEvaluatedKey")
        if not clave:
            break
        respuesta = tabla.scan(
            ProjectionExpression="#identificador",
            ExpressionAttributeNames={"#identificador": "id"},
            ExclusiveStartKey=clave
        )

    with tabla.batch_writer() as lote:
        for identificador in ids_existentes - ids_actuales:
            lote.delete_item(Key={"id": identificador})

        fecha_extraccion = datetime.now(timezone.utc).isoformat()
        for indice, registro in enumerate(registros, start=1):
            elemento = dict(registro)
            elemento["orden"] = indice
            elemento["fuente"] = URL_IGP
            elemento["fecha_extraccion_utc"] = fecha_extraccion
            lote.put_item(Item=elemento)


def serializar_json(objeto):
    if isinstance(objeto, Decimal):
        return float(objeto)
    raise TypeError(f"Tipo no serializable: {type(objeto).__name__}")


def lambda_handler(event, context):
    try:
        respuesta = requests.get(
            URL_IGP,
            headers={"User-Agent": "Mozilla/5.0 (compatible; SismosIGPBot/1.0)"},
            timeout=25
        )
        respuesta.raise_for_status()

        registros = extraer_ultimos_sismos(respuesta.text)
        if len(registros) != 10:
            return {
                "statusCode": 502,
                "body": json.dumps({
                    "error": "No se pudieron extraer los 10 reportes sísmicos. La tabla no fue modificada.",
                    "reportes_extraidos": len(registros)
                }, ensure_ascii=False)
            }

        guardar_en_dynamodb(registros)

        salida = []
        for indice, registro in enumerate(registros, start=1):
            elemento = dict(registro)
            elemento["orden"] = indice
            salida.append(elemento)

        return {
            "statusCode": 200,
            "headers": {"Content-Type": "application/json; charset=utf-8"},
            "body": json.dumps({"cantidad": len(salida), "sismos": salida}, ensure_ascii=False, default=serializar_json)
        }

    except requests.RequestException as error:
        return {
            "statusCode": 502,
            "body": json.dumps({"error": "No fue posible consultar la página del IGP.", "detalle": str(error)}, ensure_ascii=False)
        }
    except Exception as error:
        return {
            "statusCode": 500,
            "body": json.dumps({"error": "Ocurrió un error al procesar o guardar los sismos.", "detalle": str(error)}, ensure_ascii=False)
        }

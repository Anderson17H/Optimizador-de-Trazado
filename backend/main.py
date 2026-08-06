from fastapi import FastAPI, BackgroundTasks, HTTPException, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
import json
import os
import uuid
import time

# --- TUS MÓDULOS MATEMÁTICOS ---
from extractor import extraer_piezas_dxf
from doblez_optimizer import PiezaTipo, optimizar_doblez, es_pieza_grande_por_nombre

from pydantic import BaseModel
from typing import List, Optional

# 1. El ajuste individual de cada molde (tolerancia)
class MoldeAjustado(BaseModel):
    molde_id: str
    tolerancia_cm: float = 1.0

# 2. Un modelo dentro de la lista (ej. 10 Pantalones Cargo M)
class ModeloPedido(BaseModel):
    modelo_id: str
    cantidad: int
    ajustes_moldes: List[MoldeAjustado] = []

# 3. El paquete completo que envía el celular/laptop al servidor
class PeticionOptimizacion(BaseModel):
    ancho_mesa: float
    largo_mesa: float
    pedidos: List[ModeloPedido] # <--- ¡Aquí está la magia de mezclar modelos!

app = FastAPI(title="Motor de Corte - Taller")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], 
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

SISTEMA_OCUPADO = False
# Aquí guardaremos el estado de cada ticket
TRABAJOS = {}

@app.get("/")
def leer_raiz():
    return {"mensaje": "Servidor de corte operativo"}

@app.get("/api/modelos")
def obtener_modelos():
    ruta_json = os.path.join(os.path.dirname(__file__), "base_datos.json")
    try:
        with open(ruta_json, "r") as archivo:
            datos = json.load(archivo)
            return datos["modelos"]
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Base de datos no encontrada")

# ==========================================
# NUEVA LÓGICA DE OPTIMIZACIÓN ASÍNCRONA
# ==========================================

def tarea_matematica_pesada(job_id: str, payload: PeticionOptimizacion):
    global SISTEMA_OCUPADO
    ruta_json = os.path.join(os.path.dirname(__file__), "base_datos.json")
    
    def reportar_progreso(mensaje):
        if job_id in TRABAJOS:
            TRABAJOS[job_id]["progreso"] = mensaje
            print(f"[Ticket {job_id[:4]}] {mensaje}")

    try:
        reportar_progreso("Cargando recetas de la lista de pedidos...")
        
        with open(ruta_json, "r") as archivo:
            datos = json.load(archivo)
            
        moldes_db = datos["moldes"]
        piezas_tipo = []
        
        reportar_progreso("Extrayendo archivos DXF y consolidando cantidades...")
        
        # 1. Recorremos TODOS los modelos que agregaste a la pantalla
        for pedido in payload.pedidos:
            if pedido.modelo_id not in datos["modelos"]:
                raise ValueError(f"El modelo {pedido.modelo_id} no existe en la base de datos.")
                
            receta = datos["modelos"][pedido.modelo_id]["receta"]
            
            # Convertimos la lista de ajustes en un diccionario rápido para buscar
            ajustes_dict = {ajuste.molde_id: ajuste.tolerancia_cm for ajuste in pedido.ajustes_moldes}
            
            for item in receta:
                id_molde = item["molde_id"]
                if id_molde not in moldes_db:
                    continue
                    
                info_molde = moldes_db[id_molde]
                ruta_dxf = os.path.join(os.path.dirname(__file__), info_molde["ruta"])
                
                # Consolidamos cantidades (prendas * piezas por prenda)
                cantidad_total = item["cantidad_por_prenda"] * pedido.cantidad
                
                # 2. Rescatamos la desviación personalizada de esta pieza (si no hay, usa 1.0)
                tolerancia = ajustes_dict.get(id_molde, 1.0)
                
                poligonos = extraer_piezas_dxf(ruta_dxf)
                if poligonos:
                    piezas_tipo.append(PiezaTipo(
                        alias=info_molde["alias"],
                        poligono=poligonos[0],
                        cantidad_necesaria=cantidad_total,
                        es_pieza_grande=es_pieza_grande_por_nombre(info_molde["alias"]),
                        tolerancia_cm=tolerancia  # <--- Inyectamos el aplome individual
                    ))

        if not piezas_tipo:
            raise ValueError("No se encontraron piezas válidas para optimizar.")

        reportar_progreso("Iniciando motor de optimización matemática...")
        
        # 3. Usamos el ancho y largo que pusiste en la pantalla de la izquierda
        resultado = optimizar_doblez(
            piezas_tipo=piezas_tipo,
            ancho_mesa=payload.ancho_mesa,
            callback_progreso=reportar_progreso,
            largo_maximo=payload.largo_mesa
        )

        largo_final = resultado.mejor_escenario.largo_total_equivalente_cm
        eficiencia = getattr(resultado, 'eficiencia_porcentual', 0.0)

        # Éxito
        TRABAJOS[job_id] = {
            "estado": "completado",
            "progreso": "¡Optimización terminada!",
            "resultado": {
                "largo_cm": round(largo_final, 2),
                "eficiencia": round(eficiencia, 1),
                "tiempo_seg": round(resultado.tiempo_total_seg, 1)
            }
        }
        
    except Exception as e:
        print(f"Error en tarea {job_id}: {e}")
        TRABAJOS[job_id] = {"estado": "error", "detalle": str(e)}
    finally:
        SISTEMA_OCUPADO = False


@app.post("/api/optimizar")
def iniciar_optimizacion(payload: PeticionOptimizacion, background_tasks: BackgroundTasks):
    global SISTEMA_OCUPADO
    
    if SISTEMA_OCUPADO:
        raise HTTPException(status_code=429, detail="El motor ya está calculando otro tizado. Por favor espera.")
    
    SISTEMA_OCUPADO = True
    
    job_id = str(uuid.uuid4())
    TRABAJOS[job_id] = {"estado": "iniciando", "progreso": "Preparando motor..."}
    
    # Enviamos el paquete completo de datos (payload) a la función de fondo
    background_tasks.add_task(tarea_matematica_pesada, job_id, payload)
    
    return {"mensaje": "Orden recibida", "job_id": job_id}


@app.get("/api/estado/{job_id}")
def consultar_estado(job_id: str):
    """ El celular consultará esta ruta cada 5 segundos para ver cómo va """
    if job_id not in TRABAJOS:
        raise HTTPException(status_code=404, detail="Ticket no encontrado")
    
    return TRABAJOS[job_id]

# ==========================================
# NUEVAS RUTAS: FLUJO 1 (CONFIGURACIÓN)
# ==========================================

@app.post("/api/moldes/subir")
async def subir_molde_dxf(archivo: UploadFile = File(...), alias: str = "Pieza Nueva"):
    """
    Recibe un archivo .dxf desde la PC, lo guarda localmente en la carpeta almacen_dxf,
    y registra un nuevo ID de molde en el base_datos.json.
    """
    ruta_json = os.path.join(os.path.dirname(__file__), "base_datos.json")
    
    # 1. Asegurar que la carpeta de destino exista
    carpeta_dxf = os.path.join(os.path.dirname(__file__), "..", "almacen_dxf")
    os.makedirs(carpeta_dxf, exist_ok=True)
    
    # 2. Guardar el archivo físico
    ruta_archivo_fisico = os.path.join(carpeta_dxf, archivo.filename)
    try:
        with open(ruta_archivo_fisico, "wb") as buffer:
            contenido = await archivo.read()
            buffer.write(contenido)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"No se pudo guardar el archivo: {str(e)}")

    # 3. Actualizar el base_datos.json
    with open(ruta_json, "r", encoding="utf-8") as f:
        datos = json.load(f)
        
    # Generar un ID único simple para el molde (ej. DXF-002)
    nuevo_id_molde = f"DXF-{len(datos['moldes']) + 1:03d}"
    
    # Guardamos la ruta relativa limpia
    ruta_relativa = f"../almacen_dxf/{archivo.filename}"
    
    datos["moldes"][nuevo_id_molde] = {
        "alias": alias,
        "ruta": ruta_relativa
    }
    
    with open(ruta_json, "w", encoding="utf-8") as f:
        json.dump(datos, f, indent=4, ensure_ascii=False)
        
    return {"mensaje": "Molde DXF guardado con éxito", "molde_id": nuevo_id_molde}


class RecetaItem(BaseModel):
    molde_id: str
    cantidad_por_prenda: int

class NuevoModeloRequest(BaseModel):
    tipo: str  # polo, short, chompa, buzo
    nombre: str # Ej: Polo Cuello V - Talla M
    receta: list[RecetaItem]

@app.post("/api/modelos/crear")
def crear_nuevo_modelo(payload: NuevoModeloRequest):
    """
    Crea una nueva receta/modelo combinando los moldes existentes en la base de datos.
    """
    ruta_json = os.path.join(os.path.dirname(__file__), "base_datos.json")
    
    with open(ruta_json, "r", encoding="utf-8") as f:
        datos = json.load(f)
        
    # Generar un ID único para el modelo (ej. MOD-002)
    nuevo_id_modelo = f"MOD-{len(datos['modelos']) + 1:03d}"
    
    # Formatear la receta para que encaje exactamente con la estructura actual
    receta_formateada = [
        {"molde_id": item.molde_id, "cantidad_por_prenda": item.cantidad_por_prenda}
        for item in payload.receta
    ]
    
    datos["modelos"][nuevo_id_modelo] = {
        "tipo": payload.tipo,
        "nombre": payload.nombre,
        "receta": receta_formateada
    }
    
    with open(ruta_json, "w", encoding="utf-8") as f:
        json.dump(datos, f, indent=4, ensure_ascii=False)
        
    return {"mensaje": "Modelo creado con éxito", "modelo_id": nuevo_id_modelo}
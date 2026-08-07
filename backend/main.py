from fastapi import FastAPI, BackgroundTasks, HTTPException, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
import json
import os
import uuid
import time
import base64
import io
import matplotlib
matplotlib.use('Agg') # Modo servidor: dibuja en memoria, sin abrir ventanas
import matplotlib.pyplot as plt
from matplotlib.patches import Polygon as MplPolygon

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
        with open(ruta_json, "r") as archivo:
            datos = json.load(archivo)
            
        moldes_db = datos.get("moldes", {})
        
        factor = 1
        mejor_resultado_global = None
        mejor_eficiencia_global = -1
        mejor_factor = 1
        mejor_largo = 0
        mejor_tiempo = 0

        reportar_progreso("Iniciando búsqueda iterativa de lotes (Auto-escalado)...")

        # ========================================================
        # 1. BUCLE MULTIPLICADOR Y FILTRO MATEMÁTICO
        # ========================================================
        while True:
            reportar_progreso(f"Evaluando proporción de lote x{factor}...")
            piezas_tipo = []
            
            for pedido in payload.pedidos:
                if pedido.modelo_id not in datos["modelos"]:
                    raise ValueError(f"Modelo {pedido.modelo_id} no existe.")
                    
                receta = datos["modelos"][pedido.modelo_id]["receta"]
                ajustes_dict = {ajuste.molde_id: ajuste.tolerancia_cm for ajuste in pedido.ajustes_moldes}
                
                for item in receta:
                    id_molde = item["molde_id"]
                    if id_molde not in moldes_db: continue
                        
                    info_molde = moldes_db[id_molde]
                    ruta_dxf = os.path.join(os.path.dirname(__file__), info_molde["ruta"])
                    
                    # Aplicamos el multiplicador del lote actual
                    cantidad_total = item["cantidad_por_prenda"] * pedido.cantidad * factor
                    tolerancia = ajustes_dict.get(id_molde, 1.0)
                    
                    poligonos = extraer_piezas_dxf(ruta_dxf)
                    if poligonos:
                        piezas_tipo.append(PiezaTipo(
                            alias=info_molde["alias"],
                            poligono=poligonos[0],
                            cantidad_necesaria=cantidad_total,
                            es_pieza_grande=es_pieza_grande_por_nombre(info_molde["alias"]),
                            tolerancia_cm=tolerancia
                        ))

            if not piezas_tipo:
                raise ValueError("No se encontraron piezas válidas para optimizar.")

            area_total_moldes = sum(p.poligono.area * p.cantidad_necesaria for p in piezas_tipo)
            cota_inferior_largo = area_total_moldes / payload.ancho_mesa

            # Filtro Matemático
            if cota_inferior_largo > payload.largo_mesa:
                if factor == 1:
                    raise ValueError(f"Imposible: El área mínima requiere {cota_inferior_largo:.1f} cm, pero la mesa tiene {payload.largo_mesa} cm.")
                else:
                    reportar_progreso(f"Filtro matemático: Lote x{factor} supera la mesa. Deteniendo iteración.")
                    break

            try:
                resultado = optimizar_doblez(
                    piezas_tipo=piezas_tipo,
                    ancho_mesa=payload.ancho_mesa,
                    tolerancia_rot=2.0,
                    callback_progreso=reportar_progreso,
                    largo_maximo=payload.largo_mesa
                )
            except RuntimeError as e:
                if factor == 1:
                    raise ValueError(str(e))
                else:
                    reportar_progreso(f"Límite alcanzado en lote x{factor}.")
                    break

            largo_actual = resultado.mejor_escenario.largo_total_equivalente_cm
            
            if largo_actual > payload.largo_mesa:
                if factor == 1:
                    mejor_resultado_global = resultado
                    mejor_eficiencia_global = (area_total_moldes / (payload.ancho_mesa * largo_actual)) * 100
                    mejor_factor = factor
                    mejor_largo = largo_actual
                    mejor_tiempo = resultado.tiempo_total_seg
                break

            eficiencia_real = (area_total_moldes / (payload.ancho_mesa * largo_actual)) * 100
            
            if eficiencia_real > mejor_eficiencia_global:
                mejor_eficiencia_global = eficiencia_real
                mejor_resultado_global = resultado
                mejor_factor = factor
                mejor_largo = largo_actual
                mejor_tiempo = resultado.tiempo_total_seg

            factor += 1

        # ========================================================
        # 2. RENDERIZADO VISUAL EN MEMORIA (Recreando GUI)
        # ========================================================
        reportar_progreso(f"¡Lote óptimo encontrado (x{mejor_factor})! Generando gráficos...")
        
        imagenes_b64 = []
        if mejor_resultado_global:
            # Gráfico Principal
            piezas = mejor_resultado_global.piezas_resto_colocadas or []
            if piezas:
                fig, ax = plt.subplots(figsize=(8, 8))
                ax.plot([0, payload.ancho_mesa, payload.ancho_mesa, 0, 0], [0, 0, mejor_largo, mejor_largo, 0], 'r--', lw=2)

                for p_tupla in piezas:
                    p = p_tupla[0]
                    alias = p_tupla[1]
                    x, y = p.exterior.xy
                    
                    es_caja_doblez = id(p) == getattr(mejor_resultado_global, 'id_caja_doblez', None)
                    color_fondo = 'plum' if es_caja_doblez else 'lightblue'
                    
                    poligono_dibujo = MplPolygon(list(zip(x, y)), closed=True, edgecolor='black', facecolor=color_fondo, alpha=0.7)
                    ax.add_patch(poligono_dibujo)
                    
                    cx, cy = p.centroid.x, p.centroid.y
                    texto = "CAJA DOBLEZ" if es_caja_doblez else alias
                    ax.text(cx, cy, texto, ha='center', va='center', fontsize=8, fontweight='bold', color='black', wrap=True)

                ax.set_xlim(-10, payload.ancho_mesa + 10)
                ax.set_ylim(-10, mejor_largo + 20)
                ax.set_aspect('equal')
                ax.axis('off') # Diseño limpio para web
                
                buf = io.BytesIO()
                plt.savefig(buf, format='png', bbox_inches='tight', dpi=150)
                plt.close(fig)
                buf.seek(0)
                imagenes_b64.append(f"data:image/png;base64,{base64.b64encode(buf.read()).decode('utf-8')}")

            # Gráfico Secundario (Doblez)
            if mejor_resultado_global.piezas_doblez_colocadas:
                piezas_dob = mejor_resultado_global.piezas_doblez_colocadas
                largo_dob = max(p[0].bounds[3] for p in piezas_dob)
                fig2, ax2 = plt.subplots(figsize=(8, 8))
                ax2.plot([0, payload.ancho_mesa, payload.ancho_mesa, 0, 0], [0, 0, largo_dob, largo_dob, 0], 'g--', lw=2)
                
                for p_tupla in piezas_dob:
                    p = p_tupla[0]
                    alias = p_tupla[1]
                    x, y = p.exterior.xy
                    poligono_dibujo = MplPolygon(list(zip(x, y)), closed=True, edgecolor='black', facecolor='lightgreen', alpha=0.7)
                    ax2.add_patch(poligono_dibujo)
                    ax2.text(p.centroid.x, p.centroid.y, alias, ha='center', va='center', fontsize=8, fontweight='bold', color='black')

                ax2.set_xlim(-10, payload.ancho_mesa + 10)
                ax2.set_ylim(-10, largo_dob + 20)
                ax2.set_aspect('equal')
                ax2.axis('off')
                
                buf2 = io.BytesIO()
                plt.savefig(buf2, format='png', bbox_inches='tight', dpi=150)
                plt.close(fig2)
                buf2.seek(0)
                imagenes_b64.append(f"data:image/png;base64,{base64.b64encode(buf2.read()).decode('utf-8')}")

        # ========================================================
        # 3. ENVÍO DEL RESULTADO AL FRONTEND
        # ========================================================
        TRABAJOS[job_id] = {
            "estado": "completado",
            "progreso": f"¡Lote x{mejor_factor} Optimizado!",
            "resultado": {
                "largo_cm": round(mejor_largo, 2),
                "eficiencia": round(mejor_eficiencia_global, 1),
                "tiempo_seg": round(mejor_tiempo, 1),
                "imagenes": imagenes_b64
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
async def subir_molde_dxf(archivo: UploadFile = File(...)):
    ruta_json = os.path.join(os.path.dirname(__file__), "base_datos.json")
    carpeta_dxf = os.path.join(os.path.dirname(__file__), "..", "almacen_dxf")
    os.makedirs(carpeta_dxf, exist_ok=True)
    
    ruta_archivo_fisico = os.path.join(carpeta_dxf, archivo.filename)
    try:
        with open(ruta_archivo_fisico, "wb") as buffer:
            contenido = await archivo.read()
            buffer.write(contenido)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"No se pudo guardar: {str(e)}")

    # Extraemos el nombre original sin la extensión .dxf
    nombre_sin_ext = os.path.splitext(archivo.filename)[0]
    nuevo_id_molde = nombre_sin_ext # Usaremos el mismo nombre como ID
    
    with open(ruta_json, "r", encoding="utf-8") as f:
        datos = json.load(f)
        
    ruta_relativa = f"../almacen_dxf/{archivo.filename}"
    datos["moldes"][nuevo_id_molde] = {
        "alias": nombre_sin_ext,
        "ruta": ruta_relativa
    }
    
    with open(ruta_json, "w", encoding="utf-8") as f:
        json.dump(datos, f, indent=4, ensure_ascii=False)
        
    return {"mensaje": "Molde guardado", "molde_id": nuevo_id_molde}

@app.get("/api/moldes")
def obtener_moldes():
    """Devuelve la lista de moldes para llenar el desplegable del frontend"""
    ruta_json = os.path.join(os.path.dirname(__file__), "base_datos.json")
    try:
        with open(ruta_json, "r", encoding="utf-8") as f:
            datos = json.load(f)
            return datos.get("moldes", {})
    except FileNotFoundError:
        return {}


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
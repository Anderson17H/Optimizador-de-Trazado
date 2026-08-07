from fastapi import FastAPI, BackgroundTasks, HTTPException, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
import json
import os
import uuid
import time
import base64
import io
import matplotlib
matplotlib.use('Agg') 
import matplotlib.pyplot as plt
from matplotlib.patches import Polygon as MplPolygon

# --- TUS MÓDULOS MATEMÁTICOS ---
from extractor import extraer_piezas_dxf
from doblez_optimizer import PiezaTipo, optimizar_doblez, es_pieza_grande_por_nombre

from pydantic import BaseModel
from typing import List, Optional

class MoldeAjustado(BaseModel):
    molde_id: str
    tolerancia_cm: float = 1.0

class ModeloPedido(BaseModel):
    modelo_id: str
    cantidad: float 
    ajustes_moldes: List[MoldeAjustado] = []

class PeticionOptimizacion(BaseModel):
    ancho_mesa: float
    largo_mesa: float
    pedidos: List[ModeloPedido] 

app = FastAPI(title="Motor de Corte - Taller")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], 
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

SISTEMA_OCUPADO = False
TRABAJOS = {}

@app.get("/")
def leer_raiz():
    return {"mensaje": "Servidor de corte operativo"}

@app.get("/api/modelos")
def obtener_modelos():
    ruta_json = os.path.join(os.path.dirname(__file__), "base_datos.json")
    try:
        with open(ruta_json, "r", encoding="utf-8") as archivo:
            datos = json.load(archivo)
            return datos["modelos"]
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Base de datos no encontrada")

# ==========================================
# LÓGICA DE OPTIMIZACIÓN ASÍNCRONA
# ==========================================

def tarea_matematica_pesada(job_id: str, payload: PeticionOptimizacion):
    global SISTEMA_OCUPADO
    ruta_json = os.path.join(os.path.dirname(__file__), "base_datos.json")
    
    def reportar_progreso(mensaje):
        if job_id in TRABAJOS:
            TRABAJOS[job_id]["progreso"] = mensaje
            print(f"[Ticket {job_id[:4]}] {mensaje}")

    try:
        with open(ruta_json, "r", encoding="utf-8") as archivo:
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
            
            # NUEVO: Diccionario para agrupar piezas compartidas entre modelos
            piezas_agrupadas = {}
            
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
                    
                    # Calculamos la cantidad específica requerida por ESTE modelo
                    cantidad_total = item["cantidad_por_prenda"] * pedido.cantidad * factor
                    tolerancia = ajustes_dict.get(id_molde, 1.0)
                    
                    # LÓGICA DE AGRUPACIÓN: Si el molde ya existe, sumamos la cantidad
                    if id_molde in piezas_agrupadas:
                        piezas_agrupadas[id_molde]["cantidad"] += cantidad_total
                    else:
                        poligonos = extraer_piezas_dxf(ruta_dxf)
                        if poligonos:
                            piezas_agrupadas[id_molde] = {
                                "alias": info_molde["alias"],
                                "poligono": poligonos[0],
                                "cantidad": cantidad_total,
                                "tolerancia": tolerancia
                            }

            # Convertimos el diccionario agrupado a la lista final para el motor NFP
            piezas_tipo = []
            for id_m, data in piezas_agrupadas.items():
                piezas_tipo.append(PiezaTipo(
                    alias=data["alias"],
                    poligono=data["poligono"],
                    cantidad_necesaria=data["cantidad"],
                    es_pieza_grande=es_pieza_grande_por_nombre(data["alias"]),
                    tolerancia_cm=data["tolerancia"]
                ))

            if not piezas_tipo:
                raise ValueError("No se encontraron piezas válidas para optimizar.")

            area_total_moldes = sum(p.poligono.area * p.cantidad_necesaria for p in piezas_tipo)
            cota_inferior_largo = area_total_moldes / payload.ancho_mesa

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
        # 2. RENDERIZADO VISUAL ESTÉTICO (ESCARLÚ STUDIO)
        # ========================================================
        reportar_progreso(f"¡Lote óptimo encontrado (x{mejor_factor})! Generando gráficos...")
        
        plt.rcParams.update({
            'font.family': 'sans-serif',
            'font.size': 6,
            'text.color': '#1A1A1A',
            'axes.labelcolor': '#C8A359',
            'xtick.color': '#1A1A1A',
            'ytick.color': '#1A1A1A'
        })

        def aplicar_estilo_mesa(ax, ancho, largo):
            """Genera la regla milimetrada y bordes dorados"""
            ax.set_xlim(-5, ancho + 5)
            ax.set_ylim(-5, largo + 5)
            ax.set_aspect('equal')
            
            # Grilla de fondo (regla)
            ax.grid(color='#E5E7EB', linestyle='--', linewidth=0.5, zorder=0)
            
            # Ocultar marcos superior y derecho, pintar inferior e izquierdo de dorado
            ax.spines['top'].set_visible(False)
            ax.spines['right'].set_visible(False)
            ax.spines['bottom'].set_color('#C8A359')
            ax.spines['left'].set_color('#C8A359')
            
            # Etiquetas de tamaño
            ax.set_xlabel('Ancho (cm)', fontsize=6, weight='bold')
            ax.set_ylabel('Largo (cm)', fontsize=6, weight='bold')
            ax.tick_params(labelsize=5)

        imagenes_b64 = []
        if mejor_resultado_global:
            
            # --- GRÁFICO 1: MESA PRINCIPAL ---
            piezas = mejor_resultado_global.piezas_resto_colocadas or []
            if piezas:
                fig, ax = plt.subplots(figsize=(10, 10))
                
                # Borde de la tela (Dorado sólido)
                ax.plot([0, payload.ancho_mesa, payload.ancho_mesa, 0, 0], [0, 0, mejor_largo, mejor_largo, 0], color='#C8A359', linestyle='-', linewidth=1.5, zorder=1)

                for p_tupla in piezas:
                    p = p_tupla[0]
                    alias = str(p_tupla[1]).replace('_', ' ') # Limpia nombres
                    x, y = p.exterior.xy
                    
                    es_caja_doblez = id(p) == getattr(mejor_resultado_global, 'id_caja_doblez', None)
                    
                    # Colores de la marca: Dorado claro para caja, Rosa suave para moldes
                    color_fondo = '#DFBC77' if es_caja_doblez else '#FDEBED'
                    transparencia = 0.6 if es_caja_doblez else 0.85
                    
                    poligono_dibujo = MplPolygon(list(zip(x, y)), closed=True, edgecolor='#1A1A1A', facecolor=color_fondo, alpha=transparencia, linewidth=0.6, zorder=2)
                    ax.add_patch(poligono_dibujo)
                    
                    # Texto minimalista
                    cx, cy = p.centroid.x, p.centroid.y
                    texto = "CAJA DOBLEZ" if es_caja_doblez else alias
                    ax.text(cx, cy, texto, ha='center', va='center', fontsize=4.5, color='#1A1A1A', weight='normal', wrap=True, zorder=3)

                aplicar_estilo_mesa(ax, payload.ancho_mesa, mejor_largo)
                
                buf = io.BytesIO()
                # transparent=True quita el fondo blanco duro detrás de la regla
                plt.savefig(buf, format='png', bbox_inches='tight', dpi=200, transparent=True)
                plt.close(fig)
                buf.seek(0)
                imagenes_b64.append(f"data:image/png;base64,{base64.b64encode(buf.read()).decode('utf-8')}")

            # --- GRÁFICO 2: MESA DE DOBLEZ ---
            if mejor_resultado_global.piezas_doblez_colocadas:
                piezas_dob = mejor_resultado_global.piezas_doblez_colocadas
                largo_dob = max(p[0].bounds[3] for p in piezas_dob)
                
                fig2, ax2 = plt.subplots(figsize=(10, 10))
                # Borde del doblez (Dorado punteado)
                ax2.plot([0, payload.ancho_mesa, payload.ancho_mesa, 0, 0], [0, 0, largo_dob, largo_dob, 0], color='#C8A359', linestyle='--', linewidth=1.5, zorder=1)
                
                for p_tupla in piezas_dob:
                    p = p_tupla[0]
                    alias = str(p_tupla[1]).replace('_', ' ')
                    x, y = p.exterior.xy
                    
                    # Blanco hueso para piezas en el doblez
                    poligono_dibujo = MplPolygon(list(zip(x, y)), closed=True, edgecolor='#1A1A1A', facecolor='#FFF8F8', alpha=0.9, linewidth=0.6, zorder=2)
                    ax2.add_patch(poligono_dibujo)
                    ax2.text(p.centroid.x, p.centroid.y, alias, ha='center', va='center', fontsize=4.5, color='#1A1A1A', weight='normal', wrap=True, zorder=3)

                aplicar_estilo_mesa(ax2, payload.ancho_mesa, largo_dob)
                
                buf2 = io.BytesIO()
                plt.savefig(buf2, format='png', bbox_inches='tight', dpi=200, transparent=True)
                plt.close(fig2)
                buf2.seek(0)
                imagenes_b64.append(f"data:image/png;base64,{base64.b64encode(buf2.read()).decode('utf-8')}")

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
    
    background_tasks.add_task(tarea_matematica_pesada, job_id, payload)
    
    return {"mensaje": "Orden recibida", "job_id": job_id}

@app.get("/api/estado/{job_id}")
def consultar_estado(job_id: str):
    if job_id not in TRABAJOS:
        raise HTTPException(status_code=404, detail="Ticket no encontrado")
    return TRABAJOS[job_id]

# ==========================================
# RUTAS DE FLUJO 1 (CONFIGURACIÓN)
# ==========================================

@app.post("/api/moldes/subir")
async def subir_molde_dxf(archivos: List[UploadFile] = File(...)):
    ruta_json = os.path.join(os.path.dirname(__file__), "base_datos.json")
    carpeta_dxf = os.path.join(os.path.dirname(__file__), "..", "almacen_dxf")
    os.makedirs(carpeta_dxf, exist_ok=True)
    
    with open(ruta_json, "r", encoding="utf-8") as f:
        datos = json.load(f)
        
    nombres_guardados = []
    
    for archivo in archivos:
        ruta_archivo_fisico = os.path.join(carpeta_dxf, archivo.filename)
        try:
            with open(ruta_archivo_fisico, "wb") as buffer:
                contenido = await archivo.read()
                buffer.write(contenido)
        except Exception as e:
            print(f"Error guardando {archivo.filename}: {e}")
            continue

        nombre_sin_ext = os.path.splitext(archivo.filename)[0]
        datos["moldes"][nombre_sin_ext] = {
            "alias": nombre_sin_ext,
            "ruta": f"../almacen_dxf/{archivo.filename}"
        }
        nombres_guardados.append(nombre_sin_ext)
        
    with open(ruta_json, "w", encoding="utf-8") as f:
        json.dump(datos, f, indent=4, ensure_ascii=False)
        
    return {"mensaje": f"{len(nombres_guardados)} moldes guardados exitosamente", "moldes_ids": nombres_guardados}

@app.get("/api/moldes")
def obtener_moldes():
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
    tipo: str  
    nombre: str 
    receta: list[RecetaItem]

@app.post("/api/modelos/crear")
def crear_nuevo_modelo(payload: NuevoModeloRequest):
    ruta_json = os.path.join(os.path.dirname(__file__), "base_datos.json")
    
    with open(ruta_json, "r", encoding="utf-8") as f:
        datos = json.load(f)
        
    nuevo_id_modelo = f"MOD-{len(datos['modelos']) + 1:03d}"
    
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

@app.delete("/api/moldes/{molde_id}")
def eliminar_molde(molde_id: str):
    ruta_json = os.path.join(os.path.dirname(__file__), "base_datos.json")
    with open(ruta_json, "r", encoding="utf-8") as f:
        datos = json.load(f)
    
    if molde_id in datos.get("moldes", {}):
        del datos["moldes"][molde_id]
        with open(ruta_json, "w", encoding="utf-8") as f:
            json.dump(datos, f, indent=4, ensure_ascii=False)
        return {"mensaje": "Molde eliminado correctamente"}
        
    raise HTTPException(status_code=404, detail="Molde no encontrado")

@app.delete("/api/modelos/{modelo_id}")
def eliminar_modelo(modelo_id: str):
    ruta_json = os.path.join(os.path.dirname(__file__), "base_datos.json")
    with open(ruta_json, "r", encoding="utf-8") as f:
        datos = json.load(f)
    
    if modelo_id in datos.get("modelos", {}):
        del datos["modelos"][modelo_id]
        with open(ruta_json, "w", encoding="utf-8") as f:
            json.dump(datos, f, indent=4, ensure_ascii=False)
        return {"mensaje": "Modelo eliminado correctamente"}
        
    raise HTTPException(status_code=404, detail="Modelo no encontrado")

@app.put("/api/modelos/{modelo_id}")
def editar_modelo(modelo_id: str, payload: NuevoModeloRequest):
    ruta_json = os.path.join(os.path.dirname(__file__), "base_datos.json")
    with open(ruta_json, "r", encoding="utf-8") as f:
        datos = json.load(f)
        
    if modelo_id not in datos.get("modelos", {}):
        raise HTTPException(status_code=404, detail="Modelo no encontrado")
        
    receta_formateada = [
        {"molde_id": item.molde_id, "cantidad_por_prenda": item.cantidad_por_prenda}
        for item in payload.receta
    ]
    
    datos["modelos"][modelo_id] = {
        "tipo": payload.tipo,
        "nombre": payload.nombre,
        "receta": receta_formateada
    }
    
    with open(ruta_json, "w", encoding="utf-8") as f:
        json.dump(datos, f, indent=4, ensure_ascii=False)
        
    return {"mensaje": "Modelo actualizado con éxito"}
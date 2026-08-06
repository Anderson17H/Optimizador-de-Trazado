from __future__ import annotations
import logging
import math
import time
from dataclasses import dataclass
from typing import List, Optional

from shapely.geometry import Polygon
from motor_nesting import optimizar_nesting_completo, empaquetar_sub_bloque_doblez

logging.basicConfig(level=logging.INFO, format="%(asctime)s [doblez] %(message)s")
logger = logging.getLogger("doblez_optimizer")

# =====================================================================
# ESTRUCTURAS DE DATOS
# =====================================================================

@dataclass
class PiezaTipo:
    alias: str
    poligono: Polygon
    cantidad_necesaria: float
    es_pieza_grande: bool = False
    tolerancia_cm: float = 1.0

@dataclass
class ResultadoOptimizacionDoblez:
    tiempo_total_seg: float
    piezas_resto_colocadas: Optional[List[tuple]] = None
    piezas_doblez_colocadas: Optional[List[tuple]] = None
    id_caja_doblez: Optional[int] = None
    eficiencia_porcentual: float = 0.0
    
    # Clase anidada (dummy) para no romper las lecturas del main_gui.py actual
    class _MejorEscenarioDummy:
        def __init__(self, largo):
            self.largo_total_equivalente_cm = largo
    
    @property
    def mejor_escenario(self):
        largo = max([p[0].bounds[3] for p in self.piezas_resto_colocadas]) if self.piezas_resto_colocadas else 0.0
        return self._MejorEscenarioDummy(largo)

    def resumen(self) -> str:
        largo = self.mejor_escenario.largo_total_equivalente_cm
        return (
            "Optimización Directa Completada.\n"
            f"Largo total del tizado: {largo:.1f} cm\n"
            f"Tiempo de cálculo NFP: {self.tiempo_total_seg:.1f} s"
        )

# Mantenemos esta heurística por compatibilidad con el GUI actual
PALABRAS_CLAVE_PIEZA_GRANDE = (
    "delantero", "espalda", "delant", "espald", "cuerpo", "front", "back",
)

def es_pieza_grande_por_nombre(alias: str) -> bool:
    nombre = alias.lower()
    return any(palabra in nombre for palabra in PALABRAS_CLAVE_PIEZA_GRANDE)

# =====================================================================
# LÓGICA PRINCIPAL DIRECTA
# =====================================================================

def optimizar_doblez(
    piezas_tipo: List[PiezaTipo],
    ancho_mesa: float,
    tolerancia_rot: float = 2.0,
    paso_rot: float = 1,
    valor_buffer: float = 0.1,
    forzar_candidatos: Optional[List[str]] = None,
    excluir_candidatos: Optional[List[str]] = None,
    max_combinaciones_advertencia: int = 64,
    n_ordenes_busqueda: int = 2,
    max_intercambios_busqueda: int = 10,
    n_ordenes_final: int = 8,
    max_intercambios_final: int = 120,
    callback_progreso=None,
    largo_maximo: float = float('inf')
) -> ResultadoOptimizacionDoblez:
    
    t_inicio_total = time.time()
    flat_resto = []
    flat_doblez = []

    # 1. SEPARACIÓN MATEMÁTICA ESTRICTA (0.5 al doblez, enteros a la mesa plana)
    for p in piezas_tipo:
        parte_entera = math.floor(p.cantidad_necesaria)
        # Usamos round para evitar problemas de precisión flotante de Python (ej: 0.499999)
        fraccion = round(p.cantidad_necesaria % 1, 3)

        if parte_entera > 0:
            # Agregamos p.tolerancia_cm a la tupla
            flat_resto.extend([(p.poligono, p.alias, p.tolerancia_cm)] * int(parte_entera))
        
        if fraccion > 0:
            cantidad_en_caja = int(round(fraccion * 2))
            flat_doblez.extend([(p.poligono, p.alias, p.tolerancia_cm)] * cantidad_en_caja)

    piezas_doblez_internas_final = None
    id_caja = None
    super_poligono_final = None

    # 2. CREAR CAJA DE DOBLEZ (Si hay fracciones)
    if flat_doblez:
        if callback_progreso:
            callback_progreso("Calculando geometría de la caja de doblez...")
        super_poligono_final, piezas_doblez_internas_final = empaquetar_sub_bloque_doblez(
            flat_doblez, ancho_mesa, tolerancia_rot, paso_rot, valor_buffer
        )

    # 3. EMPAQUETADO ÚNICO AL MOTOR NFP
    if callback_progreso:
        callback_progreso("Empaquetando tizado principal con anclaje lateral...")

    # Enviamos los parámetros completos, incluyendo max_intercambios_final para hacer los swaps
    piezas_resto_final = optimizar_nesting_completo(
        flat_resto, ancho_mesa=ancho_mesa, tolerancia_rot=tolerancia_rot,
        paso_rot=paso_rot, valor_buffer=valor_buffer,
        n_ordenes_aleatorios=n_ordenes_final, max_intercambios=max_intercambios_final,
        callback_progreso=callback_progreso, caja_doblez=super_poligono_final,
        largo_maximo=largo_maximo
    )

    # Si el motor abortó por superar el límite (Filtro 2), devolvemos el error que el GUI espera para detener la iteración
    if not piezas_resto_final and (flat_resto or super_poligono_final):
        raise RuntimeError(f"El tizado superó el límite de la mesa ({largo_maximo} cm).")
    
    if not piezas_resto_final:
        piezas_resto_final = []

    # 4. IDENTIFICAR LA CAJA PARA PINTARLA EN EL GRÁFICO GUI
    if super_poligono_final:
        for p, alias in piezas_resto_final:
            if alias == "CAJA COMPARTIDA / DOBLEZ":
                id_caja = id(p)
                break

    tiempo_total = time.time() - t_inicio_total
    logger.info(f"Tizado único completado en {tiempo_total:.1f}s")

    return ResultadoOptimizacionDoblez(
        tiempo_total_seg=tiempo_total,
        piezas_resto_colocadas=piezas_resto_final,
        piezas_doblez_colocadas=piezas_doblez_internas_final,
        id_caja_doblez=id_caja,
    )
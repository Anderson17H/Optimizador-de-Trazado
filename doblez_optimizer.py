from __future__ import annotations

import contextlib
import io
import logging
import math
import time
import concurrent.futures
from dataclasses import dataclass, field
from itertools import combinations, chain
from typing import List, Optional, Sequence

from shapely.geometry import Polygon
from shapely.affinity import translate

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


@dataclass
class BloqueResultado:
    piezas_colocadas: Optional[List[tuple]]
    largo_cm: float


@dataclass
class EscenarioDoblez:
    alias_en_doblez: List[str]
    alias_en_bloque_normal: List[str]
    largo_bloque_normal_cm: float
    largo_bloque_doblez_cm: float
    largo_total_equivalente_cm: float
    factible: bool
    tiempo_evaluacion_seg: float = 0.0

    @property
    def usa_doblez(self) -> bool:
        return len(self.alias_en_doblez) > 0


@dataclass
class ResultadoOptimizacionDoblez:
    mejor_escenario: EscenarioDoblez
    escenario_sin_doblez: EscenarioDoblez
    ahorro_cm: float
    ahorro_porcentual: float
    total_combinaciones_evaluadas: int
    tiempo_total_seg: float
    piezas_resto_colocadas: Optional[List[tuple]] = None
    piezas_doblez_colocadas: Optional[List[tuple]] = None
    id_caja_doblez: Optional[int] = None
    todos_los_escenarios: List[EscenarioDoblez] = field(default_factory=list)

    def resumen(self) -> str:
        m = self.mejor_escenario
        if not m.usa_doblez:
            return (
                "No conviene usar doblez para este conjunto de piezas.\n"
                f"Largo de trazado (sin doblez): {self.escenario_sin_doblez.largo_total_equivalente_cm:.1f} cm"
            )
        return (
            "CONVIENE usar doblez.\n"
            f"Piezas al bloque de doblez:  {', '.join(m.alias_en_doblez)}\n"
            f"Piezas en bloque normal:     {', '.join(m.alias_en_bloque_normal) or '(ninguna)'}\n"
            f"Largo bloque normal:  {m.largo_bloque_normal_cm:.1f} cm\n"
            f"Largo bloque doblez:  {m.largo_bloque_doblez_cm:.1f} cm\n"
            f"Largo total (con doblez):  {m.largo_total_equivalente_cm:.1f} cm\n"
            f"Largo total (sin doblez):  {self.escenario_sin_doblez.largo_total_equivalente_cm:.1f} cm\n"
            f"Ahorro: {self.ahorro_cm:.1f} cm ({self.ahorro_porcentual:.1f}%)\n"
            f"Combinaciones evaluadas: {self.total_combinaciones_evaluadas}\n"
            f"Tiempo total de búsqueda: {self.tiempo_total_seg:.1f} s"
        )


# =====================================================================
# HEURÍSTICA DE "ES PIEZA GRANDE"
# =====================================================================

PALABRAS_CLAVE_PIEZA_GRANDE = (
    "delantero", "espalda", "delant", "espald", "cuerpo", "front", "back",
)

def es_pieza_grande_por_nombre(alias: str) -> bool:
    nombre = alias.lower()
    return any(palabra in nombre for palabra in PALABRAS_CLAVE_PIEZA_GRANDE)


# =====================================================================
# HELPERS INTERNOS
# =====================================================================

def _expandir(pieza, es_doblez: bool, solo_fraccion: bool = False) -> list:
    """Devuelve una lista de tuplas (Polígono, Alias) con control matemático estricto."""
    # math.floor asegura que 1.5 extraiga exactamente 1 parte entera, no 2.
    parte_entera = math.floor(pieza.cantidad_necesaria)
    fraccion = pieza.cantidad_necesaria % 1

    if solo_fraccion:
        return [(pieza.poligono, pieza.alias)] * int(round(fraccion * 2))
    if es_doblez:
        return [(pieza.poligono, pieza.alias)] * int(round(pieza.cantidad_necesaria * 2))
    else:
        return [(pieza.poligono, pieza.alias)] * parte_entera


def _largo_de(piezas_colocadas) -> float:
    if not piezas_colocadas:
        return float("inf")
    # Utilizamos p[0] porque ahora viajamos con tuplas (Poligono, Alias)
    return max(p[0].bounds[3] for p in piezas_colocadas)


def _correr_nesting_silencioso(piezas_flat: List[tuple], ancho_mesa: float,
                                tolerancia_rot: float, paso_rot: float,
                                valor_buffer: float, n_ordenes_aleatorios: int,
                                max_intercambios: int) -> BloqueResultado:
    if not piezas_flat:
        return BloqueResultado(piezas_colocadas=[], largo_cm=0.0)

    buffer_nulo = io.StringIO()
    with contextlib.redirect_stdout(buffer_nulo):
        resultado = optimizar_nesting_completo(
            piezas_flat,
            ancho_mesa=ancho_mesa,
            tolerancia_rot=tolerancia_rot,
            paso_rot=paso_rot,
            valor_buffer=valor_buffer,
            n_ordenes_aleatorios=n_ordenes_aleatorios,
            max_intercambios=max_intercambios,
        )

    return BloqueResultado(piezas_colocadas=resultado, largo_cm=_largo_de(resultado))


def _powerset(items: Sequence[str]):
    return chain.from_iterable(combinations(items, r) for r in range(len(items) + 1))


def _evaluar_combinacion(indice_combo, combo, candidatos, no_candidatos, ancho_mesa,
                          tolerancia_rot, paso_rot, valor_buffer,
                          n_ordenes_busqueda, max_intercambios_busqueda,
                          mejor_conocido_cm=None):
    t0 = time.time()
    alias_doblez = set(combo)

    piezas_doblez_tipo = [p for p in candidatos if p.alias in alias_doblez]
    piezas_resto_tipo = no_candidatos + [p for p in candidatos if p.alias not in alias_doblez]

    flat_resto = [poly for p in piezas_resto_tipo for poly in _expandir(p, es_doblez=False)]
    flat_doblez = [poly for p in piezas_doblez_tipo for poly in _expandir(p, es_doblez=True)]

    # RESTAURADO: Las fracciones huérfanas regresan a la caja de doblez
    for p in piezas_resto_tipo:
        if p.cantidad_necesaria % 1 != 0:
            flat_doblez.extend(_expandir(p, es_doblez=True, solo_fraccion=True))

    # --- Poda temprana ---
    cota_inferior_cm = (
        sum(pz[0].area for pz in flat_resto) + sum(pz[0].area for pz in flat_doblez)
    ) / ancho_mesa

    if mejor_conocido_cm is not None and cota_inferior_cm >= mejor_conocido_cm:
        return EscenarioDoblez(
            alias_en_doblez=sorted(alias_doblez),
            alias_en_bloque_normal=sorted(p.alias for p in piezas_resto_tipo),
            largo_bloque_normal_cm=float('inf'),
            largo_bloque_doblez_cm=0.0,
            largo_total_equivalente_cm=float('inf'),
            factible=False,
            tiempo_evaluacion_seg=time.time() - t0,
        )

    largo_total = float('inf')
    factible = False
    largo_caja_doblez = 0.0

    if flat_doblez:
        super_poligono, piezas_internas = empaquetar_sub_bloque_doblez(
            flat_doblez, ancho_mesa, tolerancia_rot, paso_rot, valor_buffer
        )
        if super_poligono:
            largo_caja_doblez = super_poligono[0].bounds[3] - super_poligono[0].bounds[1]
            
            # 1. Empaquetamos SOLO las piezas normales de mesa plana
            resultado_unificado = _correr_nesting_silencioso(
                flat_resto, ancho_mesa, tolerancia_rot, paso_rot, valor_buffer,
                n_ordenes_busqueda, max_intercambios_busqueda
            )
            
            # 2. El largo total es el tizado normal + la caja anclada en el tope
            largo_normal = resultado_unificado.largo_cm
            largo_total = largo_normal + largo_caja_doblez
            factible = largo_normal != float('inf')
    else:
        resultado_unificado = _correr_nesting_silencioso(
            flat_resto, ancho_mesa, tolerancia_rot, paso_rot, valor_buffer,
            n_ordenes_busqueda, max_intercambios_busqueda
        )
        largo_total = resultado_unificado.largo_cm
        factible = largo_total != float('inf')

    escenario = EscenarioDoblez(
        alias_en_doblez=sorted(alias_doblez),
        alias_en_bloque_normal=sorted(p.alias for p in piezas_resto_tipo),
        largo_bloque_normal_cm=largo_total,
        largo_bloque_doblez_cm=largo_caja_doblez,
        largo_total_equivalente_cm=largo_total,
        factible=factible,
        tiempo_evaluacion_seg=time.time() - t0,
    )

    logger.info(
        "Combo doblez=%s -> largo_total=%.1f cm (caja doblez=%.1f) [%.1fs] %s",
        escenario.alias_en_doblez or "(ninguna)",
        largo_total if factible else float("inf"),
        largo_caja_doblez, escenario.tiempo_evaluacion_seg,
        "" if factible else "[NO FACTIBLE o DESCARTADA POR PODA]",
    )

    return escenario

# =====================================================================
# LÓGICA PRINCIPAL
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
    n_ordenes_final: int = 4,
    max_intercambios_final: int = 80,
    callback_progreso=None,
) -> ResultadoOptimizacionDoblez:
    
    forzar_candidatos = set(forzar_candidatos or [])
    excluir_candidatos = set(excluir_candidatos or [])

    candidatos = [
        p for p in piezas_tipo
        if p.alias not in excluir_candidatos
        and (not p.es_pieza_grande or p.alias in forzar_candidatos)
    ]
    no_candidatos = [p for p in piezas_tipo if p not in candidatos]

    todos_los_escenarios: List[EscenarioDoblez] = []
    t_inicio_total = time.time()

    lista_combos = list(_powerset([p.alias for p in candidatos]))
    combo_vacio = next(c for c in lista_combos if len(c) == 0)
    lista_combos.remove(combo_vacio)

    if callback_progreso:
        callback_progreso("Evaluando escenario sin doblez (referencia)...")

    escenario_sin_doblez_result = _evaluar_combinacion(
        0, combo_vacio, candidatos, no_candidatos, ancho_mesa,
        tolerancia_rot, paso_rot, valor_buffer,
        n_ordenes_busqueda, max_intercambios_busqueda,
    )
    todos_los_escenarios.append(escenario_sin_doblez_result)

    mejor_conocido_cm = (
        escenario_sin_doblez_result.largo_total_equivalente_cm
        if escenario_sin_doblez_result.factible else float('inf')
    )

    if callback_progreso:
        callback_progreso(f"Evaluando {len(lista_combos)} combinaciones restantes en paralelo...")

    with concurrent.futures.ProcessPoolExecutor() as executor:
        futuros = {
            executor.submit(
                _evaluar_combinacion, idx + 1, combo, candidatos, no_candidatos, ancho_mesa,
                tolerancia_rot, paso_rot, valor_buffer,
                n_ordenes_busqueda, max_intercambios_busqueda,
                mejor_conocido_cm,
            ): combo
            for idx, combo in enumerate(lista_combos)
        }

        completados = 0
        for futuro in concurrent.futures.as_completed(futuros):
            escenario = futuro.result()
            todos_los_escenarios.append(escenario)
            completados += 1

            if escenario.factible and escenario.largo_total_equivalente_cm < mejor_conocido_cm:
                mejor_conocido_cm = escenario.largo_total_equivalente_cm

            if callback_progreso:
                callback_progreso(
                    f"Evaluadas {completados}/{len(lista_combos)} combinaciones "
                    f"(mejor hasta ahora: {mejor_conocido_cm:.1f} cm)..."
                )

    tiempo_total = time.time() - t_inicio_total

    escenarios_factibles = [e for e in todos_los_escenarios if e.factible]
    if not escenarios_factibles:
        raise RuntimeError(
            "Ninguna combinación logró colocar todas las piezas. Revisa el ancho o la tolerancia."
        )

    escenario_sin_doblez = next(e for e in escenarios_factibles if not e.alias_en_doblez)
    mejor = min(escenarios_factibles, key=lambda e: e.largo_total_equivalente_cm)

    ahorro_cm = escenario_sin_doblez.largo_total_equivalente_cm - mejor.largo_total_equivalente_cm
    ahorro_pct = (
        (ahorro_cm / escenario_sin_doblez.largo_total_equivalente_cm * 100)
        if escenario_sin_doblez.largo_total_equivalente_cm > 0 else 0.0
    )

    # --- Corrida final de calidad completa sobre la combinación ganadora ---
    if callback_progreso:
        callback_progreso("Recalculando la mejor combinación con calidad de producción...")

    alias_doblez_ganador = set(mejor.alias_en_doblez)
    piezas_doblez_tipo = [p for p in candidatos if p.alias in alias_doblez_ganador]
    piezas_resto_tipo = no_candidatos + [p for p in candidatos if p.alias not in alias_doblez_ganador]

    # 1. Piezas asignadas a la mesa plana
    flat_resto = [poly for p in piezas_resto_tipo for poly in _expandir(p, es_doblez=False)]
        
    # 2. Piezas asignadas al bloque de doblez
    flat_doblez = [poly for p in piezas_doblez_tipo for poly in _expandir(p, es_doblez=True)]
        
    # 3. Fracciones huérfanas
    for p in piezas_resto_tipo:
         if p.cantidad_necesaria % 1 != 0:
             flat_doblez.extend(_expandir(p, es_doblez=True, solo_fraccion=True))

    piezas_doblez_internas_final = None
    id_caja = None
    super_poligono_final = None
    piezas_doblez_final = None  # <--- VARIABLE INICIALIZADA AQUÍ

    if flat_doblez:
        super_poligono_final, piezas_doblez_internas_final = empaquetar_sub_bloque_doblez(
            flat_doblez, ancho_mesa, tolerancia_rot, paso_rot, valor_buffer
        )
        if super_poligono_final:
            piezas_doblez_final = piezas_doblez_internas_final 

    # 1. Empaquetar SOLO las piezas de mesa plana
    piezas_resto_final = optimizar_nesting_completo(
        flat_resto, ancho_mesa=ancho_mesa, tolerancia_rot=tolerancia_rot,
        paso_rot=paso_rot, valor_buffer=valor_buffer,
        n_ordenes_aleatorios=n_ordenes_final, max_intercambios=max_intercambios_final,
        callback_progreso=callback_progreso
    )
    
    if not piezas_resto_final:
        piezas_resto_final = []

    # 2. LA MAGIA FÍSICA: Si hay caja de doblez, la anclamos en el LADO SUPERIOR
    if super_poligono_final:
        # Calculamos dónde terminaron de acomodarse las piezas normales
        largo_normal = max([p[0].bounds[3] for p in piezas_resto_final]) if piezas_resto_final else 0.0
        
        caja_poly = super_poligono_final[0]
        alias_caja = super_poligono_final[1]
        
        # Trasladamos la caja para que descanse exactamente como una banda transversal superior
        caja_trasladada = translate(caja_poly, xoff=0.0, yoff=largo_normal)
        
        id_caja = id(caja_trasladada)
        piezas_resto_final.append((caja_trasladada, alias_caja))

    return ResultadoOptimizacionDoblez(
        mejor_escenario=mejor,
        escenario_sin_doblez=escenario_sin_doblez,
        ahorro_cm=max(ahorro_cm, 0.0),
        ahorro_porcentual=max(ahorro_pct, 0.0),
        total_combinaciones_evaluadas=len(todos_los_escenarios),
        tiempo_total_seg=tiempo_total,
        piezas_resto_colocadas=piezas_resto_final,
        piezas_doblez_colocadas=piezas_doblez_final,
        id_caja_doblez=id_caja,
        todos_los_escenarios=todos_los_escenarios,
    )
import pyclipper
import math
import random
from shapely.affinity import rotate, translate
from shapely.geometry import Polygon, box
from shapely.ops import unary_union

# ==========================================================
# 1. MOTOR MATEMÁTICO NFP CON CACHÉ Y REDUCCIÓN
# ==========================================================
FACTOR_ESCALA = 10000
_nfp_cache = {}

def obtener_firma_poligono(poly: Polygon):
    return tuple((round(x, 2), round(y, 2)) for x, y in poly.exterior.coords)

def reducir_pieza(pieza: Polygon, valor_buffer: float) -> Polygon:
    pieza_reducida = pieza.buffer(-valor_buffer)
    if pieza_reducida.is_empty:
        pieza_reducida = pieza.buffer(-valor_buffer / 4)
    if pieza_reducida.is_empty:
        pieza_reducida = pieza
    return pieza_reducida

def calcular_nfp_cacheado(pieza_fija_origen: Polygon, pieza_movil_origen: Polygon) -> list:
    firma_fija = obtener_firma_poligono(pieza_fija_origen)
    firma_movil = obtener_firma_poligono(pieza_movil_origen)
    clave = (firma_fija, firma_movil)

    if clave not in _nfp_cache:
        movil_invertida = [[-x, -y] for x, y in pieza_movil_origen.exterior.coords]
        ruta_fija = [[int(x * FACTOR_ESCALA), int(y * FACTOR_ESCALA)] for x, y in pieza_fija_origen.exterior.coords]
        ruta_movil = [[int(x * FACTOR_ESCALA), int(y * FACTOR_ESCALA)] for x, y in movil_invertida]
        
        resultado_clipper = pyclipper.MinkowskiSum(ruta_movil, ruta_fija, True)
        
        poligonos_nfp = []
        for trayectoria in resultado_clipper:
            if len(trayectoria) >= 3: 
                coords_reales = [(x / FACTOR_ESCALA, y / FACTOR_ESCALA) for x, y in trayectoria]
                poli = Polygon(coords_reales).buffer(0)
                if not poli.is_empty:
                    if poli.geom_type == 'MultiPolygon':
                        poligonos_nfp.extend(list(poli.geoms))
                    else:
                        poligonos_nfp.append(poli)
        _nfp_cache[clave] = poligonos_nfp

    return _nfp_cache[clave]

# ==========================================================
# 2. ROTACIÓN TRIGONOMÉTRICA (APLOME) Y ORDENAMIENTO
# ==========================================================
def generar_angulos_por_aplome(tolerancia_cm, alto_molde, paso_rot=1):
    if tolerancia_cm <= 0:
        return [0, 180]
        
    max_rad = math.atan((2.0 * tolerancia_cm) / alto_molde)
    max_deg = math.degrees(max_rad)
    
    angulos = []
    num_pasos = int(max_deg / paso_rot)
    
    for base in [0, 180]:
        angulos.append(base)
        for i in range(1, num_pasos + 1):
            angulos.append(base + (i * paso_rot))
            angulos.append(base - (i * paso_rot))
            
    return list(set(angulos))

def generar_ordenes_estrategicos(piezas):
    por_area = sorted(piezas, key=lambda p: p[0].area, reverse=True)
    por_altura = sorted(piezas, key=lambda p: (p[0].bounds[3] - p[0].bounds[1]), reverse=True)
    
    intercalado = []
    if len(por_area) > 2:
        izq, der = 0, len(por_area) - 1
        usar_izq = True
        while izq <= der:
            if usar_izq:
                intercalado.append(por_area[izq]); izq += 1
            else:
                intercalado.append(por_area[der]); der -= 1
            usar_izq = not usar_izq
    else:
        intercalado = por_area[:]
        
    return [por_area, por_altura, intercalado]

# ==========================================================
# 3. LÓGICA DE EMPAQUETADO EXACTO Y PODA ANTICIPADA
# ==========================================================
def empaquetar_un_orden(piezas_ordenadas, ancho_mesa, tolerancia_cm, paso_rot, valor_buffer, largo_maximo=float('inf'), caja_doblez=None, pos_x_caja=0.0):
    piezas_colocadas_finales = []
    memoria_mesa = []

    # --- INYECCIÓN DE LA CAJA DE DOBLEZ COMO OBSTÁCULO INICIAL ---
    if caja_doblez is not None:
        caja_poly = caja_doblez[0]
        alias_caja = caja_doblez[1]
        
        # 1. Llevar la caja al origen real (X=0, Y=0) para el escudo NFP
        minx, miny, _, _ = caja_poly.bounds
        caja_origen = translate(caja_poly, xoff=-minx, yoff=-miny)
        caja_origen_reducida = reducir_pieza(caja_origen, valor_buffer)
        
        # 2. La caja ubicada físicamente donde se dibujará
        caja_ubicada = translate(caja_origen, xoff=pos_x_caja, yoff=0.0)
        
        # 3. Guardar en memoria el poligono origen para el NFP, y el X, Y real
        memoria_mesa.append((caja_ubicada, caja_origen_reducida, pos_x_caja, 0.0))
        piezas_colocadas_finales.append((caja_ubicada, alias_caja))
    # --------------------------------------------------------------------  

    for pieza_tupla in piezas_ordenadas:
        pieza = pieza_tupla[0]
        alias = pieza_tupla[1]
        
        mejor_y_global = float('inf')
        mejor_x_global = float('inf')
        mejor_pieza_ubicada = None
        mejor_pieza_reducida = None
        
        alto_molde = pieza.bounds[3] - pieza.bounds[1]
        angulos_a_probar = generar_angulos_por_aplome(tolerancia_cm, alto_molde, paso_rot)

        for angulo in angulos_a_probar:
            pieza_rotada = rotate(pieza, angulo, origin='center')
            
            minx, miny, maxx, maxy = pieza_rotada.bounds
            pieza_origen = translate(pieza_rotada, xoff=-minx, yoff=-miny)
            ancho_pieza = maxx - minx
            
            if not memoria_mesa:
                mejor_pieza_ubicada = pieza_origen
                mejor_pieza_reducida = reducir_pieza(pieza_origen, valor_buffer)
                mejor_x_global = 0.0
                mejor_y_global = 0.0
                break 
            
            pieza_movil_reducida = reducir_pieza(pieza_origen, valor_buffer)
            zonas_prohibidas = []
            
            for _, pieza_fija_reducida, fx, fy in memoria_mesa:
                poligonos_nfp_origen = calcular_nfp_cacheado(pieza_fija_reducida, pieza_movil_reducida)
                for poly in poligonos_nfp_origen:
                    zonas_prohibidas.append(translate(poly, xoff=fx, yoff=fy))
            
            if zonas_prohibidas:
                zonas_limpias = [z.buffer(0) for z in zonas_prohibidas]
                gran_zona_prohibida = unary_union(zonas_limpias)
                
                puntos_validos = []
                geometrias = [gran_zona_prohibida] if gran_zona_prohibida.geom_type == 'Polygon' else gran_zona_prohibida.geoms
                
                for geom in geometrias:
                    puntos_validos.extend(list(geom.exterior.coords))
                    for interior in geom.interiors:
                        puntos_validos.extend(list(interior.coords))
                
                puntos_filtrados = []
                for px, py in puntos_validos:
                    if px >= 0 and (px + ancho_pieza - (valor_buffer*2)) <= ancho_mesa and py >= 0:
                        puntos_filtrados.append((px, py))
                
                if puntos_filtrados:
                    puntos_filtrados.sort(key=lambda p: (p[1], p[0]))
                    mejor_x_angulo, mejor_y_angulo = puntos_filtrados[0]
                    
                    if mejor_y_angulo < mejor_y_global or (mejor_y_angulo == mejor_y_global and mejor_x_angulo < mejor_x_global):
                        mejor_y_global = mejor_y_angulo
                        mejor_x_global = mejor_x_angulo
                        mejor_pieza_ubicada = translate(pieza_origen, xoff=mejor_x_angulo, yoff=mejor_y_angulo)
                        mejor_pieza_reducida = pieza_movil_reducida
        
        if mejor_pieza_ubicada is not None:
            memoria_mesa.append((mejor_pieza_ubicada, mejor_pieza_reducida, mejor_x_global, mejor_y_global))
            piezas_colocadas_finales.append((mejor_pieza_ubicada, alias))
        else:
            minx, miny, _, _ = pieza.bounds
            pieza_origen = translate(pieza, xoff=-minx, yoff=-miny)
            largo_actual = max([p[0].bounds[3] for p in piezas_colocadas_finales]) if piezas_colocadas_finales else 0
            pieza_emergencia = translate(pieza_origen, xoff=0, yoff=largo_actual)
            pieza_emergencia_reducida = reducir_pieza(pieza_origen, valor_buffer)
            
            memoria_mesa.append((pieza_emergencia, pieza_emergencia_reducida, 0.0, largo_actual))
            piezas_colocadas_finales.append((pieza_emergencia, alias))

        # --- FILTRO 2 (PODA DE PERMUTACIÓN) ---
        largo_actual_tizado = max([p[0].bounds[3] for p in piezas_colocadas_finales])
        if largo_actual_tizado > largo_maximo:
            return None  # Aborta este orden específico inmediatamente

    return piezas_colocadas_finales

def optimizar_nesting_completo(piezas_originales, ancho_mesa, tolerancia_rot=2.0, paso_rot=1,
                                valor_buffer=0.1, n_ordenes_aleatorios=4, max_intercambios=15,
                                callback_progreso=None, largo_maximo=float('inf'), caja_doblez=None):
    
    if not piezas_originales and caja_doblez is not None:
        minx, miny, _, _ = caja_doblez[0].bounds
        return [(translate(caja_doblez[0], xoff=-minx, yoff=-miny), caja_doblez[1])]
        
    if not piezas_originales:
        return []

    ordenes = generar_ordenes_estrategicos(piezas_originales)
    ordenes_a_evaluar = ordenes[:n_ordenes_aleatorios] if n_ordenes_aleatorios < 3 else ordenes

    mejor_resultado_global = None
    mejor_largo_global = float('inf')

    # --- RECUPERAMOS LAS 5 POSICIONES EN X ---
    posiciones_caja = [0.0]
    if caja_doblez is not None:
        ancho_caja = caja_doblez[0].bounds[2] - caja_doblez[0].bounds[0]
        max_x = ancho_mesa - ancho_caja
        
        if max_x > 0.1:
            posiciones_caja = []
            num_pasos = 5
            for i in range(num_pasos):
                pos_x = (max_x * i) / (num_pasos - 1)
                posiciones_caja.append(pos_x)
    # -----------------------------------------

    for pos_x in posiciones_caja:
        mejor_orden_local = None
        mejor_largo_local = float('inf')
        
        # --- FASE 1: HEURÍSTICA BASE ---
        for idx, orden in enumerate(ordenes_a_evaluar):
            if callback_progreso:
                msg = f"Estrategia {idx+1}/{len(ordenes_a_evaluar)}"
                if caja_doblez:
                    msg += f" | Caja en X: {pos_x:.1f}"
                callback_progreso(msg)
                
            resultado_prueba = empaquetar_un_orden(orden, ancho_mesa, tolerancia_rot, paso_rot, valor_buffer, largo_maximo, caja_doblez, pos_x)
            
            if resultado_prueba is None:
                continue
            
            largo_prueba = max([p[0].bounds[3] for p in resultado_prueba]) if resultado_prueba else float('inf')
            
            if largo_prueba < mejor_largo_local:
                mejor_largo_local = largo_prueba
                mejor_orden_local = orden
                
            if largo_prueba < mejor_largo_global:
                mejor_largo_global = largo_prueba
                mejor_resultado_global = resultado_prueba

        # --- FASE 2: COMPRESIÓN E INTERLOCKING ---
        if mejor_orden_local is not None and max_intercambios > 0 and len(mejor_orden_local) > 1:
            orden_base = list(mejor_orden_local)
            
            for i in range(max_intercambios):
                if callback_progreso:
                    msg_swap = f"Compresión {i+1}/{max_intercambios}"
                    if caja_doblez:
                        msg_swap += f" | Caja en X: {pos_x:.1f}"
                    callback_progreso(msg_swap)
                
                import random
                idx1, idx2 = random.sample(range(len(orden_base)), 2)
                orden_mutado = list(orden_base)
                orden_mutado[idx1], orden_mutado[idx2] = orden_mutado[idx2], orden_mutado[idx1]
                
                resultado_prueba = empaquetar_un_orden(orden_mutado, ancho_mesa, tolerancia_rot, paso_rot, valor_buffer, largo_maximo, caja_doblez, pos_x)
                
                if resultado_prueba is None:
                    continue
                
                largo_prueba = max([p[0].bounds[3] for p in resultado_prueba]) if resultado_prueba else float('inf')
                
                if largo_prueba < mejor_largo_local:
                    mejor_largo_local = largo_prueba
                    mejor_orden_local = orden_mutado
                    orden_base = orden_mutado
                    
                if largo_prueba < mejor_largo_global:
                    mejor_largo_global = largo_prueba
                    mejor_resultado_global = resultado_prueba

    return mejor_resultado_global
# ==========================================================
# 4. LÓGICA DE NEGOCIO Y SUB-BLOQUES
# ==========================================================
def empaquetar_sub_bloque_doblez(piezas_doblez, ancho_mesa, tolerancia_rot, paso_rot, valor_buffer):
    if not piezas_doblez:
        return None, []
    
    # Empaquetamos los moldes verdaderos a tamaño real sin caja ni límite
    resultado_sub = optimizar_nesting_completo(piezas_doblez, ancho_mesa, tolerancia_rot, paso_rot, valor_buffer, n_ordenes_aleatorios=1, max_intercambios=0, callback_progreso=None)
    
    if not resultado_sub:
        return None, []
        
    minx = min([p[0].bounds[0] for p in resultado_sub])
    miny = min([p[0].bounds[1] for p in resultado_sub])
    maxx = max([p[0].bounds[2] for p in resultado_sub])
    maxy = max([p[0].bounds[3] for p in resultado_sub])
    
    ancho_caja = maxx - minx
    largo_caja_desdoblada = maxy - miny
    
    # Partimos la longitud de la caja por la mitad para la mesa principal.
    largo_caja_mesa = largo_caja_desdoblada / 2.0
    super_poligono = box(0, 0, ancho_caja, largo_caja_mesa)
    
    return (super_poligono, "CAJA COMPARTIDA / DOBLEZ"), resultado_sub
import pyclipper
import math
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
    # CORREGIDO: Se añadió [0] para leer el área y la altura del Polígono en la tupla
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
# 3. LÓGICA DE EMPAQUETADO EXACTO
# ==========================================================
def empaquetar_un_orden(piezas_ordenadas, ancho_mesa, tolerancia_cm, paso_rot, valor_buffer):
    piezas_colocadas_finales = []
    memoria_mesa = []

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

    return piezas_colocadas_finales

def optimizar_nesting_completo(piezas_originales, ancho_mesa, tolerancia_rot=2.0, paso_rot=1,
                                valor_buffer=0.1, n_ordenes_aleatorios=4, max_intercambios=15,
                                callback_progreso=None):
    if not piezas_originales:
        return []

    ordenes = generar_ordenes_estrategicos(piezas_originales)
    ordenes_a_evaluar = ordenes[:n_ordenes_aleatorios] if n_ordenes_aleatorios < 3 else ordenes

    mejor_resultado = None
    mejor_largo = float('inf')

    for idx, orden in enumerate(ordenes_a_evaluar):
        if callback_progreso:
            callback_progreso(f"Estrategia de ordenamiento {idx+1}/{len(ordenes_a_evaluar)}...")
            
        resultado_prueba = empaquetar_un_orden(orden, ancho_mesa, tolerancia_rot, paso_rot, valor_buffer)
        
        # CORREGIDO: Se añadió [0] para leer bounds del polígono en la tupla
        largo_prueba = max([p[0].bounds[3] for p in resultado_prueba]) if resultado_prueba else float('inf')
        
        if largo_prueba < mejor_largo:
            mejor_largo = largo_prueba
            mejor_resultado = resultado_prueba

    return mejor_resultado

# ==========================================================
# 4. LÓGICA DE NEGOCIO (DOBLEZ E HÍBRIDOS)
# ==========================================================
def empaquetar_sub_bloque_doblez(piezas_doblez, ancho_mesa, tolerancia_rot, paso_rot, valor_buffer):
    if not piezas_doblez:
        return None, []
    
    # 1. Empaquetamos los moldes verdaderos a tamaño real
    resultado_sub = optimizar_nesting_completo(piezas_doblez, ancho_mesa, tolerancia_rot, paso_rot, valor_buffer, n_ordenes_aleatorios=1, max_intercambios=0, callback_progreso=None)
    
    if not resultado_sub:
        return None, []
        
    minx = min([p[0].bounds[0] for p in resultado_sub])
    miny = min([p[0].bounds[1] for p in resultado_sub])
    maxx = max([p[0].bounds[2] for p in resultado_sub])
    maxy = max([p[0].bounds[3] for p in resultado_sub])
    
    ancho_caja = maxx - minx
    largo_caja_desdoblada = maxy - miny
    
    # 2. LA MAGIA FÍSICA: Partimos la longitud de la caja por la mitad para la mesa principal.
    # Al desdoblar este bloque en el taller, recuperará el 'largo_caja_desdoblada' real.
    largo_caja_mesa = largo_caja_desdoblada / 2.0
    super_poligono = box(0, 0, ancho_caja, largo_caja_mesa)
    
    return (super_poligono, "CAJA COMPARTIDA / DOBLEZ"), resultado_sub

def generar_molde_hibrido(poligono1, poligono2):
    minx1, miny1, maxx1, maxy1 = poligono1.bounds
    cx1, cy1 = (minx1 + maxx1) / 2.0, (miny1 + maxy1) / 2.0
    
    minx2, miny2, maxx2, maxy2 = poligono2.bounds
    cx2, cy2 = (minx2 + maxx2) / 2.0, (miny2 + maxy2) / 2.0
    
    dx = cx1 - cx2
    dy = cy1 - cy2
    
    poligono2_centrado = translate(poligono2, xoff=dx, yoff=dy)
    molde_hibrido = unary_union([poligono1, poligono2_centrado])
    
    if molde_hibrido.geom_type == 'MultiPolygon':
        molde_hibrido = max(molde_hibrido.geoms, key=lambda a: a.area)
        
    return molde_hibrido
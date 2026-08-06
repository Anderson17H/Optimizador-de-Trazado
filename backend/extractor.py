import ezdxf
from shapely.geometry import Polygon

def extraer_piezas_dxf(ruta_archivo):
    """
    Lee un archivo .dxf, extrae las polilíneas cerradas y las convierte
    en una lista de polígonos de Shapely ordenados por área descendente.
    """
    try:
        # Cargar el archivo DXF
        doc = ezdxf.readfile(ruta_archivo)
        msp = doc.modelspace()
        
        poligonos = []
        
        # Iterar sobre las entidades del DXF
        # Para corte láser/textil, lo ideal es trabajar con polilíneas cerradas (LWPOLYLINE)
        for entidad in msp.query('LWPOLYLINE'):
            if entidad.is_closed or entidad.closed:
                # Extraer los vértices (x, y) ignorando el eje Z
                puntos = [(punto[0], punto[1]) for punto in entidad.get_points()]
                
                # Crear el polígono con Shapely
                if len(puntos) >= 3:
                    poligono_crudo = Polygon(puntos)
                    poligono = poligono_crudo.simplify(0.1, preserve_topology=True)
                    
                    # Validar que el polígono sea válido geométricamente
                    if poligono.is_valid:
                        poligonos.append(poligono)

        # Ordenar la lista de polígonos de mayor a menor área (Regla de oro del nesting)
        poligonos_ordenados = sorted(poligonos, key=lambda p: p.area, reverse=True)
        
        print(f"✅ Se extrajeron y ordenaron {len(poligonos_ordenados)} piezas del archivo {ruta_archivo}.")
        return poligonos_ordenados

    except Exception as e:
        print(f"❌ Error al leer el archivo DXF: {e}")
        return []

# --- Bloque de prueba local ---
if __name__ == "__main__":
    # Si ejecutas este archivo directamente, probará la extracción
    # Asegúrate de poner un archivo de prueba en tu carpeta
    ruta_prueba = "dxf_in/prueba.dxf" 
    piezas = extraer_piezas_dxf(ruta_prueba)
    
    if piezas:
        for i, pieza in enumerate(piezas):
            print(f"Pieza {i+1}: Área = {pieza.area:.2f}")
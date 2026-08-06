import os
import math
import multiprocessing
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog
import matplotlib.pyplot as plt
from matplotlib.patches import Polygon as MplPolygon

from extractor import extraer_piezas_dxf
from motor_nesting import optimizar_nesting_completo
from doblez_optimizer import PiezaTipo, optimizar_doblez, es_pieza_grande_por_nombre


class Modelo:
    """Representa un modelo de prenda: su receta de piezas y cuántas prendas se quieren."""
    def __init__(self, nombre):
        self.nombre = nombre
        self.cantidad_prendas = 1
        self.piezas = []

    def agregar_pieza(self, ruta, alias, cantidad_por_prenda):
        self.piezas.append({
            "ruta": ruta,
            "alias": alias,
            "cantidad_por_prenda": cantidad_por_prenda
        })


class OptimizadorCorteApp:
    def __init__(self, root):
        self.root = root
        self.root.title("Optimizador de Corte Textil - Algoritmo Nesting")
        self.root.geometry("480x480")

        self.modelos = []          
        self.vinculos = {}         
        self.cache_geometria = {}  

        self.ancho_mesa_var = tk.DoubleVar(value=150.0)
        self.largo_mesa_var = tk.DoubleVar(value=500.0)
        self.tolerancia_var = tk.DoubleVar(value=2.0)

        self.crear_widgets()

    # ---------------------------------------------------------------
    # INTERFAZ PRINCIPAL
    # ---------------------------------------------------------------
    def crear_widgets(self):
        tk.Label(self.root, text="Parámetros de la Mesa de Corte", font=("Arial", 12, "bold")).pack(pady=10)

        frame_inputs = tk.Frame(self.root)
        frame_inputs.pack(pady=5)

        tk.Label(frame_inputs, text="Ancho del rollo (cm):").grid(row=0, column=0, sticky="e", padx=5, pady=5)
        tk.Entry(frame_inputs, textvariable=self.ancho_mesa_var, width=10).grid(row=0, column=1)

        tk.Label(frame_inputs, text="Largo de mesa (cm):").grid(row=1, column=0, sticky="e", padx=5, pady=5)
        tk.Entry(frame_inputs, textvariable=self.largo_mesa_var, width=10).grid(row=1, column=1)

        tk.Label(frame_inputs, text="Tolerancia rotación (grados):").grid(row=2, column=0, sticky="e", padx=5, pady=5)
        tk.Entry(frame_inputs, textvariable=self.tolerancia_var, width=10).grid(row=2, column=1)

        tk.Button(self.root, text="➕ Agregar Modelo", command=self.agregar_modelo,
                  bg="#4CAF50", fg="white").pack(pady=8)

        tk.Button(self.root, text="🔗 Vincular Piezas Compartidas", command=self.abrir_vinculador,
                  bg="#FF9800", fg="white").pack(pady=4)

        self.lbl_resumen = tk.Label(self.root, text="0 modelos agregados", fg="gray", justify="left")
        self.lbl_resumen.pack(pady=10)

        tk.Button(self.root, text="🚀 Optimizar Trazado (Automático)", command=self.ejecutar_nesting_con_doblez,
                  bg="#9C27B0", fg="white", font=("Arial", 12, "bold")).pack(pady=15)

        self.lbl_estado = tk.Label(self.root, text="", fg="#555555", justify="left", wraplength=440)
        self.lbl_estado.pack(pady=8)

    def actualizar_resumen(self):
        if not self.modelos:
            self.lbl_resumen.config(text="0 modelos agregados")
            return
        lineas = []
        for m in self.modelos:
            piezas_txt = ", ".join(f"{p['alias']} x{p['cantidad_por_prenda']}" for p in m.piezas)
            lineas.append(f"• {m.nombre} (x{m.cantidad_prendas}): {piezas_txt}")
        self.lbl_resumen.config(text="\n".join(lineas))

    def actualizar_estado(self, texto):
        self.lbl_estado.config(text=texto)
        self.root.update_idletasks()

    # ---------------------------------------------------------------
    # VENTANA: AGREGAR MODELO
    # ---------------------------------------------------------------
    def agregar_modelo(self):
        nombre = simpledialog.askstring("Nuevo Modelo", "Nombre del modelo (ej: Modelo 1):", parent=self.root)
        if not nombre:
            return

        cantidad = simpledialog.askfloat("Cantidad de prendas", f"¿Cuántas prendas de '{nombre}' quieres tender?",
                                            parent=self.root, minvalue=1, initialvalue=1)
        if not cantidad:
            return

        modelo = Modelo(nombre)
        modelo.cantidad_prendas = cantidad

        ventana = tk.Toplevel(self.root)
        ventana.title(f"Piezas de {nombre}")
        ventana.geometry("480x400")

        tk.Label(ventana, text=f"Receta de piezas para 1 prenda de '{nombre}'",
                 font=("Arial", 10, "bold")).pack(pady=8)

        frame_lista = tk.Frame(ventana)
        frame_lista.pack(fill="both", expand=True, padx=10)

        lista_visual = tk.Listbox(frame_lista, width=60, height=12)
        lista_visual.pack(fill="both", expand=True)

        def refrescar_lista():
            lista_visual.delete(0, tk.END)
            for p in modelo.piezas:
                lista_visual.insert(tk.END, f"{p['alias']}  x{p['cantidad_por_prenda']}  ({os.path.basename(p['ruta'])})")

        def agregar_pieza():
            ruta = filedialog.askopenfilename(title="Selecciona el molde (.dxf)",
                                               filetypes=[("Archivos DXF", "*.dxf")])
            if not ruta:
                return

            nombre_sugerido = os.path.splitext(os.path.basename(ruta))[0]
            alias = simpledialog.askstring("Nombre de la pieza",
                                            "¿Cómo se llama esta pieza? (ej: Delantero, Manga, Cuello)",
                                            parent=ventana, initialvalue=nombre_sugerido)
            if not alias:
                return

            cant_por_prenda = simpledialog.askinteger("Cantidad por prenda",
                                                        f"¿Cuántas veces se usa '{alias}' en UNA sola prenda?",
                                                        parent=ventana, minvalue=1, initialvalue=1)
            if not cant_por_prenda:
                return

            modelo.agregar_pieza(ruta, alias, cant_por_prenda)
            refrescar_lista()

        def guardar_modelo():
            if not modelo.piezas:
                messagebox.showwarning("Atención", "Agrega al menos una pieza antes de guardar el modelo.")
                return
            self.modelos.append(modelo)
            self.actualizar_resumen()
            ventana.destroy()

        tk.Button(ventana, text="➕ Añadir Pieza", command=agregar_pieza,
                  bg="#4CAF50", fg="white").pack(pady=8)
        tk.Button(ventana, text="✅ Guardar Modelo", command=guardar_modelo,
                  bg="#2196F3", fg="white").pack(pady=4)

        ventana.transient(self.root)
        ventana.grab_set()
        self.root.wait_window(ventana)

    # ---------------------------------------------------------------
    # VENTANA: VINCULAR PIEZAS COMPARTIDAS
    # ---------------------------------------------------------------
    def abrir_vinculador(self):
        if len(self.modelos) < 2:
            messagebox.showinfo("Vincular Piezas", "Necesitas al menos 2 modelos agregados para vincular piezas entre ellos.")
            return

        opciones = []
        for m in self.modelos:
            for p in m.piezas:
                texto = f"[{m.nombre}] {p['alias']} ({os.path.basename(p['ruta'])})"
                opciones.append((texto, p["ruta"]))

        ventana = tk.Toplevel(self.root)
        ventana.title("Vincular Piezas Compartidas")
        ventana.geometry("520x420")

        tk.Label(ventana, text="Selecciona 2 piezas que son EL MISMO molde físico\n(mismo tejido y color) y vincúlalas.",
                 font=("Arial", 9, "bold"), justify="center").pack(pady=8)

        frame_listas = tk.Frame(ventana)
        frame_listas.pack(fill="both", expand=True, padx=10, pady=5)

        tk.Label(frame_listas, text="Pieza A").grid(row=0, column=0)
        tk.Label(frame_listas, text="Pieza B").grid(row=0, column=1)

        lista_a = tk.Listbox(frame_listas, width=35, height=14, exportselection=False)
        lista_b = tk.Listbox(frame_listas, width=35, height=14, exportselection=False)
        lista_a.grid(row=1, column=0, padx=5)
        lista_b.grid(row=1, column=1, padx=5)

        for texto, _ in opciones:
            lista_a.insert(tk.END, texto)
            lista_b.insert(tk.END, texto)

        lbl_vinculos_activos = tk.Label(ventana, text="", fg="green", justify="left")
        lbl_vinculos_activos.pack(pady=5)

        def refrescar_vinculos_activos():
            if not self.vinculos:
                lbl_vinculos_activos.config(text="(sin vínculos activos todavía)")
                return
            lineas = ["Vínculos activos:"]
            for ruta_origen, ruta_rep in self.vinculos.items():
                lineas.append(f"  {os.path.basename(ruta_origen)}  ==  {os.path.basename(ruta_rep)}")
            lbl_vinculos_activos.config(text="\n".join(lineas))

        def vincular():
            sel_a = lista_a.curselection()
            sel_b = lista_b.curselection()
            if not sel_a or not sel_b:
                messagebox.showwarning("Atención", "Selecciona una pieza en cada lista.")
                return

            ruta_a = opciones[sel_a[0]][1]
            ruta_b = opciones[sel_b[0]][1]

            if ruta_a == ruta_b:
                messagebox.showwarning("Atención", "Selecciona dos archivos distintos para vincular.")
                return

            representante = self.obtener_representante(ruta_a)
            self.vinculos[ruta_b] = representante
            refrescar_vinculos_activos()

        tk.Button(ventana, text="🔗 Vincular seleccionadas", command=vincular,
                  bg="#FF9800", fg="white").pack(pady=8)

        refrescar_vinculos_activos()
        ventana.transient(self.root)
        ventana.grab_set()
        self.root.wait_window(ventana)

    def obtener_representante(self, ruta):
        visto = set()
        actual = ruta
        while actual in self.vinculos and actual not in visto:
            visto.add(actual)
            actual = self.vinculos[actual]
        return actual

    # ---------------------------------------------------------------
    # CONSOLIDACIÓN DE PIEZAS
    # ---------------------------------------------------------------
    def _consolidar_por_representante(self, factor_multiplicador=1.0):
        conteo_por_representante = {}
        alias_por_representante = {}

        for modelo in self.modelos:
            for p in modelo.piezas:
                representante = self.obtener_representante(p["ruta"])
                cantidad_necesaria = p["cantidad_por_prenda"] * modelo.cantidad_prendas * factor_multiplicador
                conteo_por_representante[representante] = (
                    conteo_por_representante.get(representante, 0) + cantidad_necesaria
                )
                alias_por_representante.setdefault(representante, p["alias"])

        return conteo_por_representante, alias_por_representante

    def calcular_piezas_totales(self, factor_multiplicador=1.0):
        conteo_por_representante, _ = self._consolidar_por_representante(factor_multiplicador)
        todas_las_piezas = []
        for ruta_representante, cantidad_total in conteo_por_representante.items():
            if ruta_representante not in self.cache_geometria:
                self.cache_geometria[ruta_representante] = extraer_piezas_dxf(ruta_representante)
            poligonos_del_molde = self.cache_geometria[ruta_representante]
            if poligonos_del_molde:
                cantidad_segura = int(round(cantidad_total))
                todas_las_piezas.extend([poligonos_del_molde[0]] * cantidad_segura)
        return todas_las_piezas

    def calcular_piezas_por_tipo(self, factor_multiplicador=1.0):
        conteo_por_representante, alias_por_representante = self._consolidar_por_representante(factor_multiplicador)
        piezas_tipo = []
        
        cantidades_restantes = conteo_por_representante.copy()

        for ruta_representante, cantidad_total in cantidades_restantes.items():
            if cantidad_total <= 0:
                continue 
                
            if ruta_representante not in self.cache_geometria:
                self.cache_geometria[ruta_representante] = extraer_piezas_dxf(ruta_representante)
                
            poligonos_del_molde = self.cache_geometria[ruta_representante]
            if poligonos_del_molde:
                alias = alias_por_representante.get(ruta_representante, os.path.basename(ruta_representante))
                piezas_tipo.append(PiezaTipo(
                    alias=alias,
                    poligono=poligonos_del_molde[0],
                    cantidad_necesaria=cantidad_total,
                    es_pieza_grande=es_pieza_grande_por_nombre(alias),
                ))
                
        return piezas_tipo

    # ---------------------------------------------------------------
    # VISUALIZACIÓN
    # ---------------------------------------------------------------
    def visualizar_resultado(self, piezas_finales, ancho_mesa, largo_total, titulo=None):
        fig, ax = plt.subplots(figsize=(10, 6))
        ax.plot([0, ancho_mesa, ancho_mesa, 0, 0], [0, 0, largo_total, largo_total, 0], 'r--', lw=2, label="Límites de Mesa")

        for p in piezas_finales:
            x, y = p.exterior.xy
            poligono_dibujo = MplPolygon(list(zip(x, y)), closed=True, edgecolor='black', facecolor='lightblue', alpha=0.7)
            ax.add_patch(poligono_dibujo)

        ax.set_xlim(-10, ancho_mesa + 10)
        ax.set_ylim(-10, largo_total + 20)
        ax.set_aspect('equal')
        ax.set_title(titulo or f"Tizada Optimizada | Largo utilizado: {largo_total:.2f} cm")
        ax.set_xlabel("Ancho (X)")
        ax.set_ylabel("Largo (Y)")
        plt.grid(True, linestyle=':', alpha=0.6)
        plt.show()

    def visualizar_resultado_doblez(self, resultado_doblez, ancho_mesa):
        piezas = resultado_doblez.piezas_resto_colocadas or []
        if not piezas:
            messagebox.showinfo("Doblez", "No hay piezas colocadas para graficar.")
            return

        largo = max(p[0].bounds[3] for p in piezas)
        fig, ax = plt.subplots(figsize=(8, 8))
        ax.plot([0, ancho_mesa, ancho_mesa, 0, 0], [0, 0, largo, largo, 0], 'r--', lw=2)

        for p_tupla in piezas:
            p = p_tupla[0]
            alias = p_tupla[1]
            x, y = p.exterior.xy
            
            es_caja_doblez = id(p) == getattr(resultado_doblez, 'id_caja_doblez', None)
            color_fondo = 'plum' if es_caja_doblez else 'lightblue'
            
            poligono_dibujo = MplPolygon(list(zip(x, y)), closed=True, edgecolor='black', facecolor=color_fondo, alpha=0.7)
            ax.add_patch(poligono_dibujo)
            
            cx, cy = p.centroid.x, p.centroid.y
            texto = "CAJA DOBLEZ" if es_caja_doblez else alias
            ax.text(cx, cy, texto, ha='center', va='center', fontsize=8, fontweight='bold', color='black', wrap=True)

        ax.set_xlim(-10, ancho_mesa + 10)
        ax.set_ylim(-10, largo + 20)
        ax.set_aspect('equal')
        eficiencia = getattr(resultado_doblez, 'eficiencia_porcentual', 0.0)
        ax.set_title(f"Tizado Principal | Largo: {largo:.2f} cm | Eficiencia: {eficiencia:.1f}%")
        
        if resultado_doblez.piezas_doblez_colocadas:
            piezas_dob = resultado_doblez.piezas_doblez_colocadas
            largo_dob = max(p[0].bounds[3] for p in piezas_dob)
            fig2, ax2 = plt.subplots(figsize=(8, 8))
            ax2.plot([0, ancho_mesa, ancho_mesa, 0, 0], [0, 0, largo_dob, largo_dob, 0], 'g--', lw=2, label="Caja Doblez")
            
            for p_tupla in piezas_dob:
                p = p_tupla[0]
                alias = p_tupla[1]
                x, y = p.exterior.xy
                poligono_dibujo = MplPolygon(list(zip(x, y)), closed=True, edgecolor='black', facecolor='lightgreen', alpha=0.7)
                ax2.add_patch(poligono_dibujo)
                cx, cy = p.centroid.x, p.centroid.y
                ax2.text(cx, cy, alias, ha='center', va='center', fontsize=8, fontweight='bold', color='black')

            ax2.set_xlim(-10, ancho_mesa + 10)
            ax2.set_ylim(-10, largo_dob + 20)
            ax2.set_aspect('equal')
            ax2.set_title(f"Detalle Interior: Bloque de Doblez | (Las piezas están x2)")
        
        plt.show()

    # ---------------------------------------------------------------
    # EJECUCIÓN DEL NESTING CON DOBLEZ
    # ---------------------------------------------------------------
    def ejecutar_nesting_con_doblez(self):
        if not self.modelos:
            messagebox.showwarning("Atención", "Agrega al menos un modelo antes de optimizar.")
            return

        ancho = self.ancho_mesa_var.get()
        tolerancia = self.tolerancia_var.get()
        largo_maximo = self.largo_mesa_var.get() 

        print("\n--- INICIANDO BUCLE DE ITERACIÓN DE LOTES ---")
        self.actualizar_estado("Iniciando búsqueda iterativa de cantidades...")

        factor = 1
        mejor_resultado_global = None
        mejor_eficiencia_global = -1
        mejor_factor = 1

        while True:
            self.actualizar_estado(f"Evaluando proporción de lote x{factor}...")
            
            piezas_tipo = self.calcular_piezas_por_tipo(factor_multiplicador=factor)
            
            if not piezas_tipo:
                messagebox.showerror("Error", "No se pudieron extraer piezas válidas.")
                return

            area_total_moldes = sum(p.poligono.area * p.cantidad_necesaria for p in piezas_tipo)

            # --- FILTRO 1: PODA MATEMÁTICA ---
            cota_inferior_largo = area_total_moldes / ancho
            if cota_inferior_largo > largo_maximo:
                print(f"Filtro matemático activado: Lote x{factor} requiere mínimo {cota_inferior_largo:.2f} cm.")
                if factor == 1:
                    self.actualizar_estado("")
                    messagebox.showerror("Error", f"Ni siquiera un Lote x1 cabe en la mesa. Requiere mínimo {cota_inferior_largo:.2f} cm.")
                    return
                break

            try:
                resultado = optimizar_doblez(
                    piezas_tipo=piezas_tipo,
                    ancho_mesa=ancho,
                    tolerancia_rot=tolerancia,
                    callback_progreso=self.actualizar_estado,
                    largo_maximo=largo_maximo 
                )
            except RuntimeError as e:
                if factor == 1:
                    self.actualizar_estado("")
                    messagebox.showerror("Error", str(e))
                    return
                else:
                    print(f"Freno activado por saturación en iteración x{factor}.")
                    break 

            largo_actual = resultado.mejor_escenario.largo_total_equivalente_cm
            
            if largo_actual > largo_maximo:
                if factor == 1:
                    mejor_resultado_global = resultado
                    resultado.eficiencia_porcentual = (area_total_moldes / (ancho * largo_actual)) * 100
                    mejor_factor = factor
                break

            eficiencia = (area_total_moldes / (ancho * largo_actual)) * 100
            resultado.eficiencia_porcentual = eficiencia
            
            print(f"Lote x{factor} -> Largo: {largo_actual:.2f} cm | Eficiencia: {eficiencia:.2f}%")

            if eficiencia > mejor_eficiencia_global:
                mejor_eficiencia_global = eficiencia
                mejor_resultado_global = resultado
                mejor_factor = factor

            factor += 1

        self.actualizar_estado("")

        if not mejor_resultado_global:
            return

        resumen = mejor_resultado_global.resumen()
        resumen += f"\n\n--- OPTIMIZACIÓN ITERATIVA ---"
        resumen += f"\nProporción ganadora: Lote x{mejor_factor}"
        resumen += f"\nEficiencia máxima lograda: {mejor_eficiencia_global:.1f}%"

        print("\n" + resumen)
        messagebox.showinfo("Resultado de Optimización Unificada", resumen)

        self.visualizar_resultado_doblez(mejor_resultado_global, ancho)


if __name__ == "__main__":
    multiprocessing.freeze_support()
    root = tk.Tk()
    app = OptimizadorCorteApp(root)
    root.mainloop()
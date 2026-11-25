import math
import os
import random
import socket
import sys
import threading
import webbrowser

from flask import Flask, render_template, request
from flask_socketio import SocketIO, emit, join_room


def recurso(ruta):
    """Ruta de un archivo (templates, static): funciona con `python server.py` y dentro del .exe."""
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, ruta)


app = Flask(__name__, template_folder=recurso("templates"), static_folder=recurso("static"))
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")

# ---------- Ajustes (tocá acá para cambiar la sensación del juego) ----------
ANCHO, ALTO = 900, 600
CX, CY = 450.0, 300.0                 # centro de la pista
ESPINA_X0, ESPINA_X1 = 250.0, 650.0   # la pista es un óvalo alrededor de este segmento
R_CENTRO = 150.0                      # radio de la línea central de la pista
SEMI_ANCHO = 60.0                     # mitad del ancho de la pista (hasta el borde exterior de los pianos)
RADIO_AUTO = 8.0
VUELTAS = 6
NECESARIOS = 3                        # corredores para largar 
VEL_MAX = 2.4                         # px por frame (a 60 fps)
GIRO = 0.036                          # radianes por frame al girar a toda velocidad
ACEL = 0.03                           # qué rápido recupera la velocidad máxima
TURBO_FACTOR, TURBO_DUR, TURBO_ESPERA = 1.45, 1.2, 6.0   # velocidad extra, segundos de turbo y de espera
PAUSA_INICIO, PAUSA_REANUDAR = 3.0, 3.0                  # cuenta regresiva (largada y al reanudar)
ESPERA_TRAS_GANADOR = 10.0            # segundos que tienen los demás para llegar después del ganador

N_BACHES, N_ACEITES, N_CONOS = 1, 1, 2
R_BACHE, R_ACEITE, R_CONO = 14.0, 16.0, 7.0
BACHE_DUR, BACHE_FRENO = 1.1, 0.6     # segundos descontrolado / cuánta velocidad conserva al caer
ACEITE_DUR = 0.9                      # segundos que gira el auto sobre el aceite (una vuelta completa)
CONO_CAIDO = 4.0                      # segundos que un cono queda tumbado

ROLES = {1: "Rojo", 2: "Azul", 3: "Amarillo", 4: "Verde"}
COLORES = {1: "#ff4d4d", 2: "#2bb8ff", 3: "#ffd23f", 4: "#3ddc97"}
GRILLA = {1: (425, 128), 2: (425, 172), 3: (390, 128), 4: (390, 172)}  # largada 2x2 antes de la meta

jugadores = {}  # {sid: rol}

LARGO_SEMI = math.pi * R_CENTRO
LARGO_RECTA = ESPINA_X1 - ESPINA_X0
LARGO_TOTAL = 2 * LARGO_RECTA + 2 * LARGO_SEMI

estado = {
    "fase": "sala",          # sala -> carrera -> fin
    "cuenta": 0.0, "t_carrera": 0.0, "pausado": False, "completa": False,
    "listos": [], "conectados": [], "nombres": ROLES, "vueltas": VUELTAS,
    "autos": [], "obst": {"baches": [], "aceites": [], "conos": []},
    "ganador": None, "llegada": [], "ranking": [], "t_gana": 0.0,
}


def ip_local():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


URL_MANDO = f"http://{ip_local()}:5000"


# ---------- Pista ----------
def pos_pista(s, off=0.0):
    """Punto de la pista a `s` px de la meta (siguiendo el sentido de la carrera); `off` = hacia afuera (+) o adentro (-)."""
    s %= LARGO_TOTAL
    a = ESPINA_X1 - CX
    if s < a:                                   # recta de arriba, hacia la derecha
        return CX + s, CY - R_CENTRO - off
    s -= a
    if s < LARGO_SEMI:                          # curva de la derecha
        t = -math.pi / 2 + s / R_CENTRO
        return ESPINA_X1 + (R_CENTRO + off) * math.cos(t), CY + (R_CENTRO + off) * math.sin(t)
    s -= LARGO_SEMI
    if s < LARGO_RECTA:                         # recta de abajo, hacia la izquierda
        return ESPINA_X1 - s, CY + R_CENTRO + off
    s -= LARGO_RECTA
    if s < LARGO_SEMI:                          # curva de la izquierda
        t = math.pi / 2 + s / R_CENTRO
        return ESPINA_X0 + (R_CENTRO + off) * math.cos(t), CY + (R_CENTRO + off) * math.sin(t)
    s -= LARGO_SEMI
    return ESPINA_X0 + s, CY - R_CENTRO - off   # último tramo de la recta de arriba


def generar_obstaculos():
    tipos = ["b"] * N_BACHES + ["a"] * N_ACEITES + ["c"] * N_CONOS
    random.shuffle(tipos)
    paso_s = (LARGO_TOTAL - 320) / len(tipos)
    obst = {"baches": [], "aceites": [], "conos": []}
    for i, t in enumerate(tipos):
        s = 160 + i * paso_s + random.uniform(0, paso_s * 0.5)
        x, y = pos_pista(s, random.uniform(-38, 38))
        if t == "b":
            obst["baches"].append({"x": x, "y": y})
        elif t == "a":
            obst["aceites"].append({"x": x, "y": y})
        else:
            obst["conos"].append({"x": x, "y": y, "caido": 0.0})
    return obst


# ---------- Autos ----------
def nuevo_auto(r):
    x, y = GRILLA[r]
    th = math.atan2(y - CY, x - CX)
    cum = (th + math.pi / 2 + math.pi) % (2 * math.pi) - math.pi  # un poco antes de la meta
    return {"r": r, "x": float(x), "y": float(y), "a": 0.0, "v": 0.0, "dir": 0.0,
            "cum": cum, "prev": th, "vuelta": 1, "pos": r,
            "spin_t": 0.0, "bache_t": 0.0, "turbo_t": 0.0, "turbo_cd": 0.0,
            "fin": None, "snd": 0.0, "en_bache": False, "en_aceite": False}


def auto_de(r):
    return next((c for c in estado["autos"] if c["r"] == r), None)


def armar_autos():
    estado["autos"] = [nuevo_auto(r) for r in sorted(jugadores.values())]


def sonido(tipo, r=0, **extra):
    socketio.emit("sonido", {"tipo": tipo, "r": r, **extra})


def emit_marcador(a=None):
    socketio.emit("marcador", {
        "fase": estado["fase"], "ganador": estado["ganador"], "listos": estado["listos"],
        "completa": estado["completa"], "pausado": estado["pausado"], "ranking": estado["ranking"],
    }, to=a)


def preparar_sala():
    """Vuelve a la sala de espera: nueva pista y cada corredor tiene que tocar «Estoy listo»."""
    estado.update(fase="sala", cuenta=0.0, t_carrera=0.0, pausado=False, listos=[],
                  ganador=None, llegada=[], ranking=[], t_gana=0.0)
    estado["obst"] = generar_obstaculos()
    armar_autos()


def arrancar():
    preparar_sala()
    estado.update(fase="carrera", cuenta=PAUSA_INICIO)
    sonido("cuenta")


def terminar():
    estado["fase"] = "fin"
    estado["ranking"] = [c["r"] for c in sorted(estado["autos"], key=lambda c: c["pos"])]
    emit_marcador()


def mover_auto(c, dt):
    s = c["dir"] if c["fin"] is None else 0.0
    for k in ("turbo_t", "turbo_cd", "snd"):
        c[k] = max(0.0, c[k] - (dt if k == "snd" else dt / 60))
    obj = 0.0 if c["fin"] is not None else VEL_MAX * (TURBO_FACTOR if c["turbo_t"] > 0 else 1.0)
    if c["spin_t"] > 0:                         # aceite: gira una vuelta completa sin control
        c["spin_t"] = max(0.0, c["spin_t"] - dt / 60)
        c["a"] += 2 * math.pi / (ACEITE_DUR * 60) * dt
        s, obj = 0.0, obj * 0.6
    if c["bache_t"] > 0:                        # bache: el auto se descontrola y gira a medias
        c["bache_t"] = max(0.0, c["bache_t"] - dt / 60)
        c["a"] += random.uniform(-0.09, 0.09) * dt
        s, obj = s * 0.5, obj * 0.7
    c["a"] += s * GIRO * min(1.0, c["v"] / VEL_MAX + 0.25) * dt
    c["v"] = max(0.0, c["v"] + (obj - c["v"]) * min(1.0, (ACEL if obj > c["v"] else 0.05) * dt))
    c["x"] += math.cos(c["a"]) * c["v"] * dt
    c["y"] += math.sin(c["a"]) * c["v"] * dt


def muros(c):
    """Si el auto toca el borde de la pista, vuelve adentro y se desliza por la pared perdiendo velocidad."""
    lim = SEMI_ANCHO - RADIO_AUTO
    sx = min(max(c["x"], ESPINA_X0), ESPINA_X1)
    dx, dy = c["x"] - sx, c["y"] - CY
    d = math.hypot(dx, dy)
    if d < 1e-6:
        return
    off = d - R_CENTRO
    if abs(off) <= lim:
        return
    nx, ny = dx / d, dy / d  # normal hacia afuera de la pista
    obj = R_CENTRO + math.copysign(lim, off)
    c["x"], c["y"] = sx + nx * obj, CY + ny * obj
    vx, vy = math.cos(c["a"]) * c["v"], math.sin(c["a"]) * c["v"]
    vn = vx * nx + vy * ny
    if (off > 0 and vn > 0) or (off < 0 and vn < 0):
        vx, vy = vx - vn * nx, vy - vn * ny
        c["v"] = math.hypot(vx, vy) * 0.9
        if c["v"] > 0.3:
            c["a"] = math.atan2(vy, vx)
        if abs(vn) > 0.8 and c["snd"] <= 0:
            c["snd"] = 15
            sonido("muro", c["r"])


def peligros(c):
    o = estado["obst"]
    en = any((c["x"] - b["x"]) ** 2 + (c["y"] - b["y"]) ** 2 < (R_BACHE + RADIO_AUTO * 0.5) ** 2 for b in o["baches"])
    if en and not c["en_bache"]:
        c["bache_t"], c["v"] = BACHE_DUR, c["v"] * BACHE_FRENO
        sonido("bache", c["r"])
    c["en_bache"] = en
    en = any((c["x"] - b["x"]) ** 2 + (c["y"] - b["y"]) ** 2 < (R_ACEITE + RADIO_AUTO * 0.5) ** 2 for b in o["aceites"])
    if en and not c["en_aceite"] and c["spin_t"] <= 0:
        c["spin_t"] = ACEITE_DUR
        sonido("aceite", c["r"])
    c["en_aceite"] = en
    for k in o["conos"]:
        if k["caido"] > 0:
            continue
        if (c["x"] - k["x"]) ** 2 + (c["y"] - k["y"]) ** 2 < (R_CONO + RADIO_AUTO * 0.9) ** 2:
            k["caido"] = CONO_CAIDO
            c["v"] *= 0.7
            sonido("cono", c["r"])


def choques(autos):
    for i in range(len(autos)):
        for j in range(i + 1, len(autos)):
            a, b = autos[i], autos[j]
            dx, dy = b["x"] - a["x"], b["y"] - a["y"]
            d = math.hypot(dx, dy)
            if 1e-6 < d < 2 * RADIO_AUTO:
                sol = (2 * RADIO_AUTO - d) / 2
                a["x"] -= dx / d * sol; a["y"] -= dy / d * sol
                b["x"] += dx / d * sol; b["y"] += dy / d * sol
                a["v"] *= 0.96; b["v"] *= 0.96
                if a["snd"] <= 0 and b["snd"] <= 0:
                    a["snd"] = b["snd"] = 15
                    sonido("choque", a["r"])


def progreso(c):
    th = math.atan2(c["y"] - CY, c["x"] - CX)
    d = (th - c["prev"] + math.pi) % (2 * math.pi) - math.pi
    c["cum"] += d
    c["prev"] = th


def clasificar():
    e = estado
    orden = sorted(e["autos"], key=lambda c: (0, e["llegada"].index(c["r"])) if c["fin"] is not None else (1, -c["cum"]))
    for i, c in enumerate(orden):
        c["pos"] = i + 1
        c["vuelta"] = min(VUELTAS, max(1, int(c["cum"] // (2 * math.pi)) + 1))


def revisar_meta(dt):
    e = estado
    for c in e["autos"]:
        if c["fin"] is None and c["cum"] >= 2 * math.pi * VUELTAS:
            c["fin"] = round(e["t_carrera"], 2)
            e["llegada"].append(c["r"])
            sonido("meta", c["r"])
            if e["ganador"] is None:
                e["ganador"] = c["r"]
                e["t_gana"] = ESPERA_TRAS_GANADOR
                sonido("victoria", c["r"])
                socketio.emit("estado_sala", {"msg": f"¡Ganó el {ROLES[c['r']]}!"})
                emit_marcador()
    if e["ganador"] is not None:
        e["t_gana"] -= dt / 60
        if e["t_gana"] <= 0 or all(c["fin"] is not None for c in e["autos"]):
            clasificar()
            terminar()


def paso(dt):
    e = estado
    if e["cuenta"] > 0:
        antes = math.ceil(e["cuenta"])
        e["cuenta"] = max(0.0, e["cuenta"] - dt / 60)
        if e["cuenta"] == 0:
            sonido("saque")
        elif math.ceil(e["cuenta"]) != antes:
            sonido("cuenta")
        return
    e["t_carrera"] += dt / 60
    for k in e["obst"]["conos"]:
        k["caido"] = max(0.0, k["caido"] - dt / 60)
    for c in e["autos"]:
        mover_auto(c, dt)
        peligros(c)
    choques(e["autos"])
    for c in e["autos"]:
        muros(c)
        progreso(c)
    clasificar()
    revisar_meta(dt)


def bucle_juego():
    import time
    previo = time.perf_counter()
    tick = 0
    while True:
        ahora = time.perf_counter()
        dt = min((ahora - previo) * 60, 3)  # en "frames" de 1/60 s; compensa los tirones
        previo = ahora
        if estado["fase"] == "carrera" and not estado["pausado"]:
            paso(dt)
        socketio.emit("actualizar_tv", estado, to="tv")  # solo a la TV, no a los celulares
        tick += 1
        if tick % 12 == 0:  # cada celular recibe su vuelta y su puesto
            for sid, r in list(jugadores.items()):
                c = auto_de(r)
                if c:
                    socketio.emit("datos", {"vuelta": c["vuelta"], "pos": c["pos"], "n": len(estado["autos"]),
                                            "fin": c["fin"]}, to=sid)
        socketio.sleep(1 / 60)


@app.route("/")
def mando():
    return render_template("mando.html")


@app.route("/tv")
def tv():
    return render_template("tv.html")


@socketio.on("connect")
def conectar():
    if request.args.get("tipo") == "tv":
        join_room("tv")
        emit("actualizar_tv", estado)
        emit("info", {"url": URL_MANDO})
        emit_marcador(a=request.sid)
        return

    rol = 0
    if len(jugadores) < NECESARIOS:
        rol = next((r for r in ROLES if r not in jugadores.values()), 0)
    if rol:
        jugadores[request.sid] = rol
    emit("rol", {"jugador": rol, "nombre": ROLES.get(rol, ""), "color": COLORES.get(rol, "#8a97a8"),
                 "turbo_espera": TURBO_ESPERA})
    emit_marcador(a=request.sid)
    if not rol:
        return  # un celular de más no molesta la carrera en curso

    estado["conectados"] = sorted(jugadores.values())
    if len(jugadores) == NECESARIOS:
        preparar_sala()
        estado["completa"] = True
        socketio.emit("estado_sala", {"msg": "Están todos: ¡cada uno toca «Estoy listo»!"})
        emit_marcador()
    else:
        armar_autos()
        socketio.emit("estado_sala", {"msg": f"Esperando corredores ({len(jugadores)}/{NECESARIOS})"})


@socketio.on("disconnect")
def desconectar():
    if request.sid in jugadores:
        del jugadores[request.sid]
        estado["completa"] = False
        estado["conectados"] = sorted(jugadores.values())
        preparar_sala()
        socketio.emit("estado_sala", {"msg": f"Se fue un corredor. Esperando ({len(jugadores)}/{NECESARIOS})"})
        emit_marcador()


@socketio.on("volante")
def volante(data):
    c = auto_de(jugadores.get(request.sid))
    try:
        v = max(-1.0, min(1.0, float(data["v"])))
    except (KeyError, TypeError, ValueError):
        return
    if c:
        c["dir"] = v


@socketio.on("turbo")
def turbo():
    c = auto_de(jugadores.get(request.sid))
    e = estado
    if not c or e["fase"] != "carrera" or e["pausado"] or e["cuenta"] > 0 or c["fin"] is not None or c["turbo_cd"] > 0:
        return
    c["turbo_t"], c["turbo_cd"] = TURBO_DUR, TURBO_ESPERA
    c["v"] *= 1.15
    sonido("turbo", c["r"])


@socketio.on("listo")
def listo():
    rol = jugadores.get(request.sid)
    if not rol or estado["fase"] not in ("sala", "fin") or not estado["completa"]:
        return
    if rol not in estado["listos"]:
        estado["listos"].append(rol)
    if len(estado["listos"]) == NECESARIOS:  # la cuenta regresiva empieza recién cuando todos dieron el ok
        arrancar()
        socketio.emit("estado_sala", {"msg": f"¡Todos listos! {VUELTAS} vueltas"})
    else:
        socketio.emit("estado_sala", {"msg": f"Listos {len(estado['listos'])}/{NECESARIOS}. Esperando al resto…"})
    emit_marcador()


@socketio.on("pausar")
def pausar():
    if estado["fase"] != "carrera":
        return
    estado["pausado"] = not estado["pausado"]
    if not estado["pausado"]:
        estado["cuenta"] = max(estado["cuenta"], PAUSA_REANUDAR)  # cuenta regresiva para retomar
    socketio.emit("pausa", {"pausado": estado["pausado"]})
    emit_marcador()


@socketio.on("nueva")
def nueva():
    """Desde la TV: vuelve a la sala (al terminar o en pausa). Todos deben dar el ok de nuevo."""
    if estado["completa"] and (estado["fase"] == "fin" or estado["pausado"]):
        preparar_sala()
        socketio.emit("estado_sala", {"msg": "Nueva carrera: cada uno toca «Estoy listo»"})
        emit_marcador()


if __name__ == "__main__":
    try:
        print(f"\n  TV:      {URL_MANDO}/tv\n  Celular: {URL_MANDO}\n")
        print("  Para cerrar el juego, cerrá esta ventana.\n")
        socketio.start_background_task(bucle_juego)
        threading.Timer(1.2, lambda: webbrowser.open("http://127.0.0.1:5000/tv")).start()
        try:
            socketio.run(app, host="0.0.0.0", port=5000, allow_unsafe_werkzeug=True)
        except TypeError:  # versiones viejas de Flask-SocketIO no conocen ese parámetro
            socketio.run(app, host="0.0.0.0", port=5000)
    except Exception as err:  # que la ventana no desaparezca sin explicar qué pasó
        print(f"\n  No se pudo iniciar el servidor: {err}")
        print("  (¿Ya hay otra copia del juego abierta usando el puerto 5000?)")
        input("\n  Presioná Enter para cerrar...")

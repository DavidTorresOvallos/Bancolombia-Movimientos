"""
actualizador.py — trae a esta carpeta los cambios del repositorio
https://github.com/DavidTorresOvallos/Bancolombia-Movimientos

Uso (desde la carpeta de Movimientos):
    py actualizador.py              # una vez: actualiza si hay cambios
    py actualizador.py --solo-ver   # solo dice qué cambiaría, no toca nada
    py actualizador.py --cada 10    # se queda vigilando: revisa cada 10 minutos
    py actualizador.py --forzar     # si un cambio del repo choca con un cambio LOCAL del mismo
                                    # archivo, guarda tu copia en _respaldo_actualizador\\ y toma la del repo

Qué garantiza:
  * NUNCA toca config.py, chats_id.json ni nada que no esté en el repo (están en .gitignore): solo se actualizan
    los archivos que el repo rastrea. Si algún día el repo rastreara un archivo de secretos, se niega a trabajar.
  * Usa «fast-forward» únicamente: no hace merges raros, no reescribe historia, no borra tus commits ni tus
    archivos locales. Si git no puede avanzar sin pisar algo tuyo, no lo hace (salvo --forzar, con respaldo).
  * Antes de aplicar, comprueba que todos los .py nuevos compilan (sintaxis). Si alguno no compila, no toca nada.
  * No reinicia nada: avisa si hay que reiniciar Movimientos (cambió código) y qué opciones nuevas faltan en config.py
    (solo los NOMBRES, nunca los valores).
  * Nunca sube nada al repositorio (solo fetch / merge).

Códigos de salida: 0 = al día o actualizado, 1 = error (red, git, sintaxis), 2 = bloqueado por cambios locales.
"""

import argparse
import datetime
import os
import re
import shutil
import subprocess
import sys
import time

CARPETA = os.path.dirname(os.path.abspath(__file__))
REMOTO = "origin"
RAMA = "main"   # rama que se usa al preparar una carpeta sin repo propio
URL_REPO = "https://github.com/DavidTorresOvallos/Bancolombia-Movimientos.git"
LOG = os.path.join(CARPETA, "actualizador.log")
RESPALDOS = os.path.join(CARPETA, "_respaldo_actualizador")
TIMEOUT_GIT = 90  # segundos por comando git (la red puede ir lenta)

# Archivos que jamás deben estar en el repo (secretos / datos de usuarios). Si el repo los rastrea, se aborta.
_PROHIBIDOS = re.compile(r"(^|/)(config(_[^/]*)?\.py|chats_id\.json|[^/]*credenciales[^/]*)$", re.IGNORECASE)


def es_secreto(ruta):
    """config.py, config_A.py, chats_id.json, *credenciales*... pero NO los de ejemplo (config_ejemplo.py, etc.)."""
    return bool(_PROHIBIDOS.search(ruta)) and "ejemplo" not in os.path.basename(ruta).lower()
# Cambios en estos archivos obligan a reiniciar el programa para que se apliquen.
_CODIGO = re.compile(r"\.py$", re.IGNORECASE)


for _flujo in (sys.stdout, sys.stderr):  # la consola de Windows (cp1252) no soporta emojis: nunca debe tumbar el script
    try:
        _flujo.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def log(msg):
    linea = f"{datetime.datetime.now():%Y-%m-%d %H:%M:%S}  {msg}"
    print(linea, flush=True)
    try:
        if os.path.exists(LOG) and os.path.getsize(LOG) > 1_000_000:   # el vigilante corre días: el log no crece sin fin
            os.replace(LOG, LOG + ".old")
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(linea + "\n")
    except OSError:
        pass


_GIT_EXE = None


def git_exe():
    """Ruta de git. Si no está en el PATH (instalación reciente: la terminal vieja no lo ve), lo busca donde Windows lo instala."""
    global _GIT_EXE
    if _GIT_EXE:
        return _GIT_EXE
    candidatos = [shutil.which("git")]
    for var in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"):
        base = os.environ.get(var)
        if not base:
            continue
        sub = os.path.join("Programs", "Git") if var == "LOCALAPPDATA" else "Git"
        candidatos += [os.path.join(base, sub, "cmd", "git.exe"), os.path.join(base, sub, "bin", "git.exe")]
    for c in candidatos:
        if c and os.path.isfile(c):
            _GIT_EXE = c
            return c
    raise ErrorActualizador("git no está instalado o no se encuentra. Instálalo (https://git-scm.com/download/win), "
                            "cierra y vuelve a abrir la terminal, y reintenta.")


def git(*args, check=True):
    """Corre git en la carpeta. Nunca pide credenciales por consola (si faltan, falla rápido en vez de colgarse)."""
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0", GCM_INTERACTIVE="never", LC_ALL="C")
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        r = subprocess.run([git_exe(), "-C", CARPETA, *args], capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=TIMEOUT_GIT, env=env, creationflags=flags)
    except FileNotFoundError:
        raise ErrorActualizador("git no está instalado o no está en el PATH.")
    except subprocess.TimeoutExpired:
        raise ErrorActualizador(f"git {args[0]} tardó más de {TIMEOUT_GIT} s (¿sin red?).")
    if check and r.returncode != 0:
        raise ErrorActualizador(f"git {' '.join(args)} falló: {(r.stderr or r.stdout).strip()[:400]}")
    return r


class ErrorActualizador(Exception):
    pass


class Bloqueado(Exception):
    pass


# ───────────────────────── candado (que no corran dos a la vez) ─────────────────────────

class Candado:
    def __enter__(self):
        # El candado vive en la propia carpeta (no en .git): funciona aunque todavía no haya repo propio.
        self.ruta = os.path.join(CARPETA, ".actualizador.lock")
        if os.path.exists(self.ruta) and time.time() - os.path.getmtime(self.ruta) < 600:
            raise Bloqueado("Ya hay otro actualizador corriendo (candado reciente).")
        with open(self.ruta, "w") as f:
            f.write(str(os.getpid()))
        return self

    def __exit__(self, *exc):
        try:
            os.remove(self.ruta)
        except OSError:
            pass


# ───────────────────────── lógica ─────────────────────────

def cambios_locales_rastreados():
    """Archivos rastreados por el repo que tienen cambios locales (modificados o borrados)."""
    out = git("status", "--porcelain", "--untracked-files=no").stdout
    return {linea[3:].strip().strip('"').replace("\\", "/") for linea in out.splitlines() if linea.strip()}


def faltantes_rastreados():
    """Archivos que el repo rastrea y que no existen en la carpeta (borrados, con o sin 'git rm')."""
    out = git("status", "--porcelain", "--untracked-files=no").stdout
    faltan = []
    for linea in out.splitlines():
        if len(linea) > 3 and "D" in linea[:2]:
            ruta = linea[3:].strip().strip('"')
            if not os.path.exists(os.path.join(CARPETA, ruta)):
                faltan.append(ruta)
    return faltan


def sintaxis_ok(ref, archivos):
    """Compila (sin ejecutar) cada .py nuevo tal como quedará. Devuelve la lista de los que fallan."""
    malos = []
    for a in archivos:
        if not a.endswith(".py"):
            continue
        r = git("show", f"{ref}:{a}", check=False)
        if r.returncode != 0:
            continue  # archivo borrado en el remoto
        try:
            compile(r.stdout, a, "exec")
        except SyntaxError as e:
            malos.append(f"{a} (línea {e.lineno}: {e.msg})")
    return malos


def nombres_config(texto):
    return set(re.findall(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=", texto, re.MULTILINE))


def opciones_faltantes_en_config():
    """Nombres que config_ejemplo.py define y config.py no (valores NUNCA se leen ni se imprimen)."""
    ej, cf = os.path.join(CARPETA, "config_ejemplo.py"), os.path.join(CARPETA, "config.py")
    if not (os.path.exists(ej) and os.path.exists(cf)):
        return []
    try:
        with open(ej, encoding="utf-8") as f:
            a = nombres_config(f.read())
        with open(cf, encoding="utf-8") as f:
            b = nombres_config(f.read())
    except OSError:
        return []
    return sorted(a - b)


def respaldar(archivos):
    destino = os.path.join(RESPALDOS, f"{datetime.datetime.now():%Y%m%d_%H%M%S}")
    for a in archivos:
        origen = os.path.join(CARPETA, a)
        if os.path.exists(origen):
            d = os.path.join(destino, a)
            os.makedirs(os.path.dirname(d), exist_ok=True)
            shutil.copy2(origen, d)
    return destino


def elegir_url_repo():
    """Primera URL del repo que responde: la del repo de una carpeta superior (si es este mismo proyecto, ya trae
    las credenciales de git de ese PC) y si no la pública por defecto."""
    candidatas = []
    padre = git("config", "--get", "remote.origin.url", check=False).stdout.strip()
    if padre and "bancolombia-movimientos" in padre.lower():
        candidatas.append(padre)
    candidatas.append(URL_REPO)
    for url in candidatas:
        if git("ls-remote", "--heads", url, RAMA, check=False).returncode == 0:
            return url
    raise ErrorActualizador("No pude acceder al repositorio remoto (¿sin internet o sin permiso de GitHub en este PC?). "
                            f"Probé: {', '.join(candidatas)}")


def crear_repo_propio():
    """Esta carpeta no tiene su propio .git (se copió a mano, o hay un repo en una carpeta superior).
    Se arma uno AQUÍ, apuntando al repo remoto, y se deja la carpeta idéntica a la rama del repo.
    Seguro por construcción: antes de tocar nada respalda en _respaldo_actualizador las copias locales que difieren del repo;
    config.py, chats_id.json y todo lo que no sea del repo (ignorado o sin rastrear) no se tocan."""
    log("🧰 Esta carpeta no tiene repositorio git propio: lo preparo aquí (los archivos que no son del repo no se tocan).")
    url = elegir_url_repo()
    git("init")
    git("symbolic-ref", "HEAD", f"refs/heads/{RAMA}")
    if git("remote", "get-url", REMOTO, check=False).returncode == 0:
        git("remote", "set-url", REMOTO, url)
    else:
        git("remote", "add", REMOTO, url)
    git("fetch", REMOTO, RAMA)
    remoto = f"{REMOTO}/{RAMA}"
    archivos = git("ls-tree", "-r", "--name-only", remoto).stdout.splitlines()
    for a in archivos:
        if es_secreto(a):
            raise ErrorActualizador(f"El repo rastrea «{a}», que es un archivo de secretos/datos. Me niego a continuar.")
    distintos = []
    for a in archivos:
        if os.path.isfile(os.path.join(CARPETA, a)):
            propio = git("hash-object", "--", a, check=False).stdout.strip()
            del_repo = git("rev-parse", f"{remoto}:{a}", check=False).stdout.strip()
            if propio != del_repo:
                distintos.append(a)
    if distintos:
        log(f"Respaldo de {len(distintos)} archivo(s) tuyos que difieren del repo, en: {respaldar(distintos)}")
    git("checkout", "-f", "-B", RAMA, remoto)
    git("branch", f"--set-upstream-to={remoto}", RAMA, check=False)
    log(f"✅ Carpeta lista: {len(archivos)} archivo(s) del repo, en {git('rev-parse', '--short', 'HEAD').stdout.strip()}. "
        f"Se conservaron tu config.py y chats_id.json.")
    log("🔁 REINICIA Movimientos si estaba corriendo, para que tome la versión del repo.")
    if any(_CODIGO.search(a) for a in distintos):
        log("   (las versiones que tenías están en la carpeta de respaldo, por si necesitas algo de ellas)")


def actualizar(solo_ver=False, forzar=False):
    # Sin repo propio, o con un repo vacío (un `git init` sin commits): se prepara igual, se parta de donde se parta.
    sin_repo = not os.path.exists(os.path.join(CARPETA, ".git"))
    sin_commits = (not sin_repo) and git("rev-parse", "--verify", "--quiet", "HEAD", check=False).returncode != 0
    if sin_repo or sin_commits:
        if solo_ver:
            log("(--solo-ver) Esta carpeta no tiene un repositorio git propio con historial; al correr sin --solo-ver "
                "se prepara aquí y queda igual a la rama del repo (con respaldo de lo tuyo que difiera).")
            return 0
        crear_repo_propio()
        return 0

    if git("rev-parse", "--is-inside-work-tree", check=False).stdout.strip() != "true":
        raise ErrorActualizador("Esta carpeta no es un repositorio git válido (¿.git dañado?). Borra la carpeta .git y vuelve a correr: "
                                "el actualizador la prepara de nuevo.")

    # La raíz del repo debe ser ESTA carpeta. Si el repo está en una carpeta superior (clon hecho un nivel más arriba),
    # las rutas de git no coinciden con las de esta carpeta y se restaurarían/actualizarían archivos en el lugar equivocado.
    raiz = git("rev-parse", "--show-toplevel").stdout.strip()
    if os.path.normcase(os.path.realpath(raiz)) != os.path.normcase(os.path.realpath(CARPETA)):
        raise ErrorActualizador(
            f"El repositorio git está en «{raiz}», no en esta carpeta («{CARPETA}»). No se toca nada. "
            f"Clona el repo DENTRO de una carpeta propia (git clone <url> Movimientos) y copia ahí config.py y chats_id.json.")

    rama = git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    if rama == "HEAD":
        raise ErrorActualizador("Estás en HEAD desacoplado (sin rama). Haz `git checkout main` primero.")

    git("fetch", REMOTO, rama)
    remoto = f"{REMOTO}/{rama}"
    if git("rev-parse", "--verify", "--quiet", remoto, check=False).returncode != 0:
        raise ErrorActualizador(f"No existe {remoto} en el repositorio.")

    # Seguridad: ni lo que ya está rastreado ni lo que viene del repo puede ser un archivo de secretos.
    for origen in ("HEAD", remoto):
        for archivo in git("ls-tree", "-r", "--name-only", origen).stdout.splitlines():
            if es_secreto(archivo):
                raise ErrorActualizador(f"El repo ({origen}) rastrea «{archivo}», que es un archivo de secretos/datos. "
                                        f"Me niego a actualizar hasta que se saque del repo.")

    # Archivos del repo que FALTAN en la carpeta (borrados a mano, antivirus, etc.): se restauran siempre, estés o no al día.
    # Solo se devuelven los que no existen; nunca se pisa un archivo que sí está (aunque lo hayas modificado).
    faltan = faltantes_rastreados()
    if faltan:
        if solo_ver:
            log(f"(--solo-ver) Faltan {len(faltan)} archivo(s) del repo en la carpeta; se restaurarían.")
        else:
            for i in range(0, len(faltan), 50):
                git("checkout", "HEAD", "--", *faltan[i:i + 50])
            log(f"🛠 Restauré {len(faltan)} archivo(s) del repo que faltaban en la carpeta"
                + (": " + ", ".join(faltan[:6]) + (" …" if len(faltan) > 6 else "")) + ".")
            if any(_CODIGO.search(a) for a in faltan):
                log("🔁 Se restauró código: REINICIA Movimientos si estaba corriendo sin esos archivos.")

    atras, adelante = (int(x) for x in git("rev-list", "--left-right", "--count", f"{remoto}...HEAD").stdout.split())
    # (git imprime: commits que solo tiene el remoto, commits que solo tiene la local)
    if atras == 0:
        extra = f" (esta carpeta tiene {adelante} commit(s) sin subir; no se toca)" if adelante else ""
        log(f"Al día con {remoto}.{extra}")
        return 0
    if adelante:
        log(f"⚠ La carpeta tiene {adelante} commit(s) propio(s) sin subir Y el repo tiene {atras} nuevo(s) "
            f"(historias separadas). No se puede avanzar sin mezclar: resuélvelo a mano (git pull --rebase).")
        return 2

    cambiados = git("diff", "--name-status", "HEAD", remoto).stdout.strip().splitlines()
    archivos = [l.split("\t")[-1] for l in cambiados]
    log(f"Hay {atras} commit(s) nuevo(s) en {remoto}; cambian {len(archivos)} archivo(s):")
    for l in cambiados:
        log("    " + l.replace("\t", "  "))

    malos = sintaxis_ok(remoto, archivos)
    if malos:
        log("✖ NO se actualiza: estos archivos del repo no compilan (se subió algo roto):")
        for m in malos:
            log("    " + m)
        return 1

    # Archivos NUEVOS del repo que en esta carpeta ya existen sueltos (copiados a mano, sin rastrear o ignorados).
    # Si son idénticos al del repo no se pierde nada (se reemplazan solos); si son distintos cuentan como choque.
    rastreados = set(git("ls-tree", "-r", "--name-only", "HEAD").stdout.splitlines())
    sueltos_distintos, sueltos_iguales = [], []
    for l in cambiados:
        estado, ruta = l.split("\t")[0], l.split("\t")[-1]
        if estado.startswith("A") and ruta not in rastreados and os.path.isfile(os.path.join(CARPETA, ruta)):
            propio = git("hash-object", "--", ruta, check=False).stdout.strip()
            del_repo = git("rev-parse", f"{remoto}:{ruta}", check=False).stdout.strip()
            (sueltos_iguales if propio and propio == del_repo else sueltos_distintos).append(ruta)

    choques = sorted(set(archivos) & cambios_locales_rastreados()) + sorted(sueltos_distintos)
    if sueltos_iguales:
        log(f"· {len(sueltos_iguales)} archivo(s) nuevo(s) del repo ya existen aquí idénticos (se reemplazan sin pérdida).")
    if choques and not forzar:
        log("⛔ NO se actualiza: estos archivos tienen cambios LOCALES tuyos y también cambiaron en el repo:")
        for c in choques:
            log("    " + c)
        log("   Sube tus cambios al repo, o corre con --forzar (guarda antes tu copia en _respaldo_actualizador).")
        return 2

    if solo_ver:
        log("(--solo-ver: no se aplicó nada)")
        return 0

    if choques:
        log(f"Respaldo de tus copias locales en: {respaldar(choques)}")
        rastreados_locales = [c for c in choques if c in rastreados]
        if rastreados_locales:
            git("checkout", "--", *rastreados_locales)  # descarta SOLO esos archivos; luego el merge entra limpio
    for ruta in sueltos_iguales + sueltos_distintos:   # los sueltos estorban al merge: se quitan (los distintos ya están respaldados)
        try:
            os.remove(os.path.join(CARPETA, ruta))
        except OSError:
            pass

    git("merge", "--ff-only", remoto)
    log(f"✅ Actualizado a {git('rev-parse', '--short', 'HEAD').stdout.strip()}.")

    if any(_CODIGO.search(a) for a in archivos):
        log("🔁 Cambió código: REINICIA Movimientos para que tome la versión nueva (no lo reinicio solo).")
    if "config_ejemplo.py" in archivos:
        falt = opciones_faltantes_en_config()
        if falt:
            log("📝 config_ejemplo.py trae opciones que tu config.py no tiene (copia las que apliquen): " + ", ".join(falt))
    return 0


def una_vez(solo_ver, forzar):
    try:
        with Candado():
            return actualizar(solo_ver, forzar)
    except Bloqueado as e:
        log(f"· {e}")
        return 0
    except ErrorActualizador as e:
        log(f"✖ {e}")
        return 1
    except Exception as e:  # nada inesperado debe matar al vigilante: se anota y se reintenta en el próximo ciclo
        log(f"✖ Error inesperado ({type(e).__name__}): {e}")
        return 1


def main():
    p = argparse.ArgumentParser(description="Actualiza esta carpeta desde el repositorio de Movimientos.")
    p.add_argument("--solo-ver", action="store_true", help="solo mostrar qué cambiaría")
    p.add_argument("--forzar", action="store_true", help="si choca con cambios locales, respalda y toma la del repo")
    p.add_argument("--cada", type=float, metavar="MIN", help="repetir cada MIN minutos (modo vigilancia)")
    a = p.parse_args()

    if not a.cada:
        sys.exit(una_vez(a.solo_ver, a.forzar))

    log(f"Vigilando el repositorio cada {a.cada:g} min. Ctrl+C para salir.")
    try:
        while True:
            una_vez(a.solo_ver, a.forzar)
            time.sleep(max(30, a.cada * 60))
    except KeyboardInterrupt:
        log("Detenido.")


if __name__ == "__main__":
    main()

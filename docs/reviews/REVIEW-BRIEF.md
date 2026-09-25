# Revisión end-to-end de Dear Agent (rama `main`, commit `882c67f`)

## 0. Quién eres y qué queremos

Actúa como un **revisor senior** con tres sombreros: (a) ingeniero de seguridad que asume
entrada no confiable y ejecución de agentes, (b) arquitecto de sistemas que cuida la coherencia
del diseño y la mantenibilidad, y (c) ingeniero Python que revisa correctitud, tests y
documentación. No eres el autor; tu trabajo es **encontrar problemas reales, verificarlos en el
código, y arreglar los que sean seguros y de alto valor**. Sé escéptico: no confíes en las
afirmaciones de los docs ni en el brief; compruébalas.

## 1. Qué es Dear Agent (contexto)

Dear Agent es un **buzón asíncrono email-first que convierte mensajes en pull requests
producidos por agentes de código**. Un mensaje (email/JMAP/webhook) encola una tarea; un agente
(el "harness": OpenCode / Claude Code / Codex / comando custom) trabaja en un **worktree Git
aislado**; el resultado es una **rama + draft PR que un humano revisa y aterriza**.

Dos objetivos de producto: (1) **encolar es el problema difícil** (asíncrono, deduplicado,
durable, reentrega segura); (2) **creatividad cuando está ocioso** (proponer trabajo desde la
actividad del repo).

Reglas de oro (no negociables, en `AGENTS.md`):

1. **Nada de código fuente por el transporte** (solo metadatos y links); Git es el único plano
   de artefactos.
2. **El entregable es un PR**; nunca push directo a `main`; landing gateado por un humano.
3. **No reemplazar el harness**: Dear Agent lo envuelve (adaptadores), no lo forkea.
4. **Enqueue idempotente** (dedupe por id de mensaje del transporte).
5. **Proveedores pluggables** (hosted o local OpenAI-compatible); nunca depender de un vendor.
6. **Sin secretos en git** (variables de entorno / archivos untracked).
7. **Entrada no confiable**: nunca ejecutar comandos derivados de un mensaje sin allowlist o
   aprobación humana; el run corre en un worktree aislado y, donde haya, sandbox de SO.
8. **Durabilidad sobre astucia**: estado en PostgreSQL o en Git, no en la cabeza de un modelo.

Lee `AGENTS.md` completo antes de tocar nada. Es el contrato operativo.

## 2. Estado actual y qué se acaba de mergear

`main` está en el commit `882c67f` (squash del PR #134). M0–M8 están hechos (ver
`docs/roadmap.md`). Lo recién mergeado son dos slices:

- **ADR 0008 — Execution environments** (`docs/decisions/0008-execution-environment.md`):
  un run es **una sesión de entorno** (harness y `verify` comparten un contenedor); el harness
  se **inyecta** en un entorno declarado por el repo en vez de asumir toolchain.
- **ADR 0009 — Forge by API** (`docs/decisions/0009-forge-api.md`): el draft PR/MR se abre por
  **REST API** con token (GitHub/GitLab/Gitea), detrás del protocolo `Forge`, sin `gh`.

### Archivos nuevos

- `src/dear_agent/environment/__init__.py`, `bundle.py`, `descriptor.py`
- `src/dear_agent/gitplane/forge.py`
- `docs/decisions/0008-execution-environment.md`, `docs/decisions/0009-forge-api.md`
- `tests/test_harness_bundle.py`, `tests/test_environment_descriptor.py`, `tests/test_forge.py`

### Archivos modificados

- `src/dear_agent/sandbox.py` (sesión en `ContainerSandbox`)
- `src/dear_agent/executor.py` (ciclo de vida de la sesión)
- `src/dear_agent/worker_factory.py` (resolución de entorno + sesión compartida)
- `src/dear_agent/gitplane/plane.py` (`push_key` para el push)
- `deploy/base/runner-template.yaml`, `runner-sidecar-template.yaml`, `secrets.yaml`
- `tests/test_container_sandbox.py`, `test_sandbox.py`, `test_executor.py`, `test_gitplane.py`,
  `test_manifests.py`
- `docs/architecture.md`, `docs/getting-started.md`, `AGENTS.md`, `TASKS.md`

### Qué cambió, en una frase cada uno

- `ContainerSandbox` dejó de ser "un `podman run --rm` por comando": el primer `wrap()` arranca
  un contenedor vivo (`podman run -d --entrypoint sleep … infinity`) y los siguientes hacen
  `podman exec`; `close()` lo elimina. `build_wrapped_argv` sigue puro.
- `TaskExecutor` cierra la sesión en su `finally`; `build_worker` resuelve el sandbox **una vez**
  y lo comparte runner + verifier + executor (antes, bajo podman, el verifier recibía bwrap).
- `environment/descriptor.py` detecta devcontainer/EE/Containerfile/mise; si declara **imagen**,
  esa es la imagen de la sesión y se inyecta el harness automáticamente.
- `environment/bundle.py` materializa un bundle portable de OpenCode (binario musl + loader +
  `libstdc++`/`libgcc`) y lo monta; un shim en `PATH` lo ejecuta con su propio loader.
- `gitplane/forge.py` abre el PR por API (`urllib`), selección por host del remoto o
  `DEAR_AGENT_FORGE`; `GhForge` queda como fallback.
- Manifiestos: el runner monta `git-read` (clone) + `git-push` (write) y expone el secreto
  `dear-agent-forge` por `envFrom`; el `control` del sidecar los lleva y el `harness` no.

## 3. Mapa del repo

- `AGENTS.md` — contrato operativo (léelo primero).
- `TASKS.md` — estado actual, recetas y **"Known gaps (for review)"** al final.
- `docs/roadmap.md`, `docs/architecture.md`, `docs/security.md`, `docs/transports.md`,
  `docs/queue.md`, `docs/providers.md`, `docs/getting-started.md`.
- `docs/decisions/0001..0009-*.md` — ADRs (0008 y 0009 son los nuevos; 0006 aislamiento por
  contenedor, 0007 aislamiento del harness vs credenciales del runner).
- `src/dear_agent/` — control plane. Puntos calientes del review:
  `sandbox.py`, `executor.py`, `worker_factory.py`, `verify.py`, `runners/`,
  `gitplane/plane.py`, `gitplane/forge.py`, `environment/`.
- `deploy/` — Kustomize (base/components/overlays) y las plantillas de Job del runner.
- `tests/` — suite completa; los tests de manifiestos requieren `kustomize` en el PATH.

## 4. Cómo levantar el entorno

```sh
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/ruff format --check . && .venv/bin/ruff check .
.venv/bin/python -m pytest -q
```

Toda la suite debe quedar verde y `ruff` limpio **antes y después** de tus cambios.

Para el e2e local (necesita `podman`; opcional pero recomendado para reproducir):

```sh
scripts/dev-postgres.sh up          # imprime DEAR_AGENT_DATABASE_URL
export DEAR_AGENT_QUEUE=postgres
export DEAR_AGENT_DATABASE_URL='postgresql://dear-agent:dear-agent@127.0.0.1:5432/dear-agent'
export DEAR_AGENT_HARNESS=opencode
export DEAR_AGENT_ISOLATION=podman
# auth del harness montada read-only (no commitees valores):
export DEAR_AGENT_HARNESS_MOUNTS='~/.local/share/opencode/auth.json:~/.local/share/opencode/auth.json:ro'
export DEAR_AGENT_HARNESS_CONTAINER_SELINUX=disable      # en host con SELinux
export DEAR_AGENT_VERIFY_ALLOW='make test'
export GH_TOKEN=...                                       # o GITHUB_TOKEN / GITLAB_TOKEN / GITEA_TOKEN
.venv/bin/dear-agent task enqueue "<instrucciones>" \
  --repo git@github.com:dearagent-dev/dear-agent-lab-python.git --verify "make test"
.venv/bin/dear-agent run --repo /tmp/<clone> <task-id>
```

La receta completa y las tareas de ejemplo están en `TASKS.md` (sección "End-to-end test
recipe"). No hace falta correr el e2e con un LLM real; con tests unitarios/manifiestos y, si
querés, un contenedor podman, alcanza para validar los mecanismos.

## 5. Qué queremos que revises (en profundidad)

Recorre el código, no solo los ADRs. Preguntas concretas por eje:

### 5.1 Correctitud

- Ciclo de vida de la sesión: ¿puede quedar un contenedor huérfano en algún camino (excepción
  antes del `finally`, `wrap()` que falla, timeout, sidecar)? ¿`close()` es idempotente? ¿El
  contenedor se elimina en todos los casos (runner que lanza, verify que lanza, kill del Job)?
- `_ensure_started` usa `--entrypoint sleep … infinity`; ¿qué pasa con imágenes sin `sleep`?,
  ¿y con `HOME` no escribible (UBI `/root` es 0550 con `--cap-drop ALL`)? ¿La sesión necesita
  `PWD`/`HOME`/`PATH` garantizados?
- Diferencias entre correr bajo `bwrap`, `NoSandbox` y `ContainerSandbox`: ¿el verifier siempre
  comparte la sesión correcta? (Hoy **no** con `DEAR_AGENT_HARNESSES`; confirmá y evaluá si vale
  arreglarlo.)
- `environment/descriptor.py`: parseo JSONC propio (¿rompe con strings/escapes/comentarios
  anidados?); `_devcontainer_build` con `build` string/objeto/legacy; resolución de rutas
  (`Path.resolve()`) — ¿permite escapar del repo (`../../etc`)? Considera path traversal.
- `environment/bundle.py`: `podman cp` a un nombre explícito (¿resuelve symlinks de soname?),
  el shim (`LD_LIBRARY_PATH`, loader explícito), montaje de un **archivo** en
  `/usr/local/bin/<harness>` (¿lo crea podman?, ¿pisa algo?), idempotencia con `.ready`
  (¿invalida si cambia la imagen/versión?), caché compartida entre runs (¿concurrencia?).
- `gitplane/forge.py`: `parse_remote` (hosts con puerto, scp-like, paths anidados de GitLab,
  mayúsculas), `AutoForge` (¿lee `os.environ` congelado en construcción o vivo?),
  `_post` (status, JSON inválido), GitLab `Draft:` vs campo `draft`, Gitea/Forgejo.
- `GitPlane.push_key`: ¿la write key se aplica **solo** al push? ¿El clone sigue con la read
  key? ¿`env={**os.environ,...}` filtra algo?
- `worker_factory.build_runner_and_session`: orden de resolución (explicit override >
  descriptor > harness image), y que runner/verifier/executor usen el **mismo** objeto sesión.

### 5.2 Seguridad (lo más importante)

- **Entrada no confiable → ejecución**: ¿algún camino permite que un mensaje/descriptor del repo
  ejecute comandos sin allowlist/aprobación? (El descriptor y el `setup` del repo son código del
  repo: ¿se ejecutan? ¿dónde? ¿con qué privilegios?)
- **Frontera de credenciales**: la sesión comparte contenedor entre harness y control en el
  modelo de un contenedor; ¿el harness puede leer `/proc/<ppid>/environ` (DSN, `GH_TOKEN`) o la
  key montada? Contrasta con ADR 0007 y con la variante sidecar. ¿La lista de mounts/`envFrom`
  del `harness` del sidecar está limpia (sin `git-*`, sin forge, sin DSN)?
- **Forge token**: ¿se loguea en algún lado (errores, eventos, PR body)? ¿Los mensajes de error
  de `GitError` incluyen el token o el body crudo de la API?
- **Bundle**: monta binarios del host de una imagen de harness; ¿superficie de supply-chain?
  ¿`readonly`? ¿El shim inyectado en `PATH` podría ser secuestrado por el repo (que escribe el
  worktree) si `PATH` resuelve primero un binario del repo?
- **git push**: ¿`push_key` está scopeado a `dear-agent/*`? ¿El guard de rama protegida sigue
  vigente? ¿`GIT_SSH_COMMAND` con `StrictHostKeyChecking=yes`?
- **`--cap-drop ALL` + provisionar**: ¿el harness puede instalar paquetes (necesita caps)? ¿Es
  consistente con "el harness aprovisiona" del ADR 0008?
- Repasa `docs/security.md` y verifica que las afirmaciones sigan siendo ciertas tras estos
  cambios (prompt injection, contención, sin código por el transporte).

### 5.3 Diseño y consistencia

- ¿El rediseño "el entorno lo declara el repo, el harness se inyecta" es coherente en todo el
  código, o quedaron caminos que asumen la imagen del harness como entorno?
- ¿`Sandbox` (con `wrap`/`close`) sigue siendo una abstracción sana, o conviene un
  `EnvironmentSession` explícito (start/exec/close) separado de `wrap`?
- ¿Los ADRs 0008/0009 describen fielmente lo implementado (sin prometer de más)? Busca
  discrepancias doc↔código.
- ¿La selección de forge/entorno por configuración es la correcta, o hay un default peligroso?
- ¿`CommandRunner` ignorando el sandbox/sesión es un agujero de consistencia?

### 5.4 Tests

- ¿Los tests verifican comportamiento real o solo acoplan a la implementación? Los tests de
  argv del contenedor, los de `_push_env`, los de manifiestos, los del bundle con fake `_run`.
- ¿Faltan casos: descriptor malformado, path traversal, forge con status raro, bundle cacheado
  con imagen distinta, sesión que falla al arrancar, ejecutor que cierra la sesión en error?
- ¿Hay tests que pasan pero no probarían una regresión?

### 5.5 Documentación

- `docs/getting-started.md`, `docs/architecture.md`, `AGENTS.md`, `TASKS.md`: ¿están alineados
  con el código? ¿Falta documentar alguna variable de entorno nueva?
- ¿La sección "Known gaps" de `TASKS.md` es honesta y completa?

### 5.6 Los gaps conocidos (verifícalos y prioriza)

Están en `TASKS.md` → "Known gaps (for review)":

1. Routing (`DEAR_AGENT_HARNESSES`) no comparte una sola sesión con el verifier.
2. `build`/`Containerfile`/devcontainer-`build` detectado pero no construido; falta
   **Feature/prebuild**.
3. Bundle solo OpenCode, receta explícita, requiere `SELINUX=Z` + `HOME` escribible.
4. Forge: GitLab `Draft:` (asunción), sin Bitbucket; credenciales visibles al harness en el
   runner de un contenedor.
5. `command` harness ignora el sandbox/sesión.
6. Imagen del harness del sidecar es placeholder; PR in-cluster sin probar en vivo.

No todos hay que arreglarlos; decide cuáles son **seguros y de alto valor** y justifícalo.

## 6. Guardrails (respétalos)

- `AGENTS.md` es el contrato: reglas de oro, "un slice por rama", TDD, docs viajan con el código,
  sin secretos en git, PR por slice, **landing gateado por humano**.
- Trabaja en una rama nueva: `dear-agent-review-fixes` (o `dear-agent-<tema>` por arreglo).
- **No hagas push directo a `main` ni mergees.** Abre un PR y deja que un humano aterrice.
- No introduzcas dependencias nuevas sin justificarlo (hoy solo `pyyaml` y `psycopg`).
- No imprimas ni commitees secretos. Verifica con `git diff` y `.gitignore`.
- Si un cambio requiere una decisión de diseño, **no adivines**: agrega `TODO(decision)` y
  propón un ADR en el reporte.

## 7. Entregables (en este orden)

1. **Informe de revisión** en `docs/reviews/2026-09-24-review.md` (o pegado en tu respuesta si
   preferís no crear archivo): hallazgos clasificados por severidad
   (**Blocker / Major / Minor / Nit**), cada uno con `archivo:línea`, qué está mal, por qué
   importa, y la corrección concreta. Incluye los **falsos positivos que descartaste** (para que
   no se re-investiguen) y una lista de "verificado, correcto" para lo que revisaste a fondo.
2. **Arreglos**: implementa en rama aparte los que sean seguros y de alto valor, con test que
   falle antes y pase después. Corre `ruff` + `pytest` completos. Actualiza los docs/ADRs que
   correspondan.
3. **PR** (no merge) con: resumen, hallazgos arreglados vs diferidos, y el link al informe.
   Deja en la descripción qué NO tocaste y por qué.

## 8. Formato del informe (por hallazgo)

```
[Severidad] Título corto
Ubicación: path/to/file.py:NN
Qué: descripción concreta
Por qué importa: impacto (seguridad/correctitud/mantenibilidad)
Evidencia: cómo lo verificaste (comando, test, lectura)
Fix propuesto: cambio concreto; ¿lo implementaste? (sí/no + rama)
```

Termina con: (a) tabla resumen por severidad, (b) top-3 riesgos, (c) qué mejoraste y qué
dejaste pendiente con su justificación.

## 9. No hagas

- No confíes en los ADRs ni en `TASKS.md` como verdad: contradícelos si el código dice otra cosa.
- No reescribas módulos enteros por estilo; prioriza hallazgos con impacto.
- No cambies el comportamiento sin test.
- No commitees `.env`, tokens ni keys.
- No mergees.

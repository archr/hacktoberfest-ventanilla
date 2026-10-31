# Cómo contribuir

¡Gracias por ayudar! Este proyecto participa en **Hacktoberfest**. Cualquier aporte que acerque
información oficial de trámites y leyes de México a más personas es bienvenido.

## Preparar el entorno

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
cp .env.example .env              # apunta LLM_BASE_URL a tu Ollama / vLLM / LM Studio
.venv/bin/python -m app.rag.ingest   # descarga y indexa las fuentes oficiales
./scripts/dev.sh                     # http://127.0.0.1:8700
.venv/bin/python -m pytest -q        # las pruebas no requieren LLM ni red
```

Necesitas un modelo de chat con *tool calling* (probado con Qwen 3) y un modelo de embeddings
(`qwen3-embedding:0.6b`). Sin embeddings puedes indexar con `--no-embed` (solo BM25).

## Ideas para contribuir

- **Nuevas fuentes oficiales** en `config/sources.yaml`: trámites de gob.mx (CURP, pasaporte,
  acta de nacimiento), IMSS, INFONAVIT, PROFECO, leyes estatales… Si el formato es nuevo, agrega
  un parser en `app/rag/parsers.py` con su prueba.
- **Nuevos documentos** en `config/document_services.yaml` y su plantilla en
  `mock_doc_service/templates.py`: queja ante PROFECO, renuncia voluntaria, solicitud de
  constancia, etc.
- **Calidad de respuestas**: un conjunto de preguntas de evaluación con respuestas esperadas y
  fuentes correctas, y mejoras de recuperación (reranking, chunking, stemming en español).
- **UI y accesibilidad**: lectores de pantalla, modo oscuro, móvil, lenguaje claro.
- **Despliegue**: Dockerfile / docker-compose, autenticación y límites de uso.

## Reglas

- Usa solo **fuentes oficiales** (dominios `.gob.mx`, DOF, Cámara de Diputados) y deja la URL
  en `sources.yaml`.
- Incluye pruebas para la lógica nueva y verifica que `python -m pytest -q` pase.
- Nunca subas `.env`, tokens, ni archivos de `data/` (pueden contener datos personales).
- Escribe el código y los textos de la UI en español claro.

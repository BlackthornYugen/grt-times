# API Linting Guide

This project enforces API guidelines (including the Microsoft Graph Resource Modeling patterns) using [Spectral](https://stoplight.io/open-source/spectral). 

You can lint the OpenAPI schema locally using Docker to ensure your API changes comply with the required standards.

## Prerequisites

1. Ensure **Docker Desktop** (or the Docker daemon) is running.
2. Ensure you have exported the latest `openapi.json` from the FastAPI application.

## 1. Export the OpenAPI Schema
Before linting, export the current OpenAPI schema from your application so Spectral has a concrete JSON file to analyze:
```bash
uv run python export_openapi.py
```
This will produce an `openapi.json` file in the root of your project.

## 2. Run Spectral via Docker
Run the following Docker command from the root of your repository:

```bash
docker run --rm -v "$(pwd):/work" -w /work stoplight/spectral lint openapi.json
```

### What this command does:
* `docker run --rm`: Runs the container and automatically removes it when complete, preventing unused containers from piling up.
* `-v "$(pwd):/work"`: Mounts your current project directory (including `.spectral.yaml` and `openapi.json`) into the container at the `/work` path.
* `-w /work`: Sets the container's working directory to `/work`. This ensures Spectral automatically discovers the `.spectral.yaml` ruleset configuration because it sits in the working directory.
* `stoplight/spectral`: The official Spectral Docker image.
* `lint openapi.json`: Instructs Spectral to lint the `openapi.json` schema.

### How Rules Are Applied
Because we run this in the working directory mapped to your codebase, Spectral will automatically detect the `.spectral.yaml` file. This file extends the general OpenAPI guidelines alongside the Microsoft Azure API Style Guide (`azure-api-style-guide`). Any deviations (such as non-pluralized routes, missing `value` root parameters, or casing issues) will be printed directly to your console.

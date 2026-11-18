# The UI and its API in one container. Build from the repository root, because
# the API also needs scenarios/:
#   docker build -f ui/Dockerfile .
# compose.yaml does this for you.
FROM node:22-slim

WORKDIR /app/ui

# Dependencies first, so code changes don't reinstall them.
COPY ui/package.json ui/package-lock.json ./
RUN npm ci

COPY ui/ ./
COPY scenarios/ /app/scenarios/
RUN npm run build

# The repository root is /app, so the defaults for scenarios/ and ui/dist/web
# work unchanged. Bind to every interface inside the container; compose.yaml
# publishes the port on 127.0.0.1 only.
ENV HOST=0.0.0.0 \
    API_PORT=8787
EXPOSE 8787

USER node
CMD ["npm", "start"]

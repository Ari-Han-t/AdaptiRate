# Build from the repository root (the SDK lives in ./sdk-node).
FROM node:22-alpine AS sdk
WORKDIR /sdk-node
COPY sdk-node/package.json sdk-node/package-lock.json ./
RUN npm ci
COPY sdk-node/tsconfig.json ./
COPY sdk-node/src ./src
COPY sdk-node/test ./test
RUN npm run build

FROM node:22-alpine
ENV NODE_ENV=production
WORKDIR /app
COPY --from=sdk /sdk-node /sdk-built
COPY acceptance/services/llm_gateway/package.json ./
RUN npm install --omit=dev --no-audit --no-fund --install-links /sdk-built
COPY acceptance/services/llm_gateway/server.js ./
USER node
EXPOSE 3000
CMD ["node", "server.js"]

FROM node:24-alpine

RUN apk add --no-cache openssl

ENV NODE_ENV=production
WORKDIR /app

COPY package*.json ./
RUN npm ci --omit=dev

COPY server.js ./
COPY public ./public

# The cert volume mounts here; owned by the unprivileged user so the server
# can generate and read its key without running as root.
RUN mkdir -p /app/certs && chown -R node:node /app/certs
USER node

EXPOSE 8443
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD wget -q --no-check-certificate -O /dev/null https://127.0.0.1:${PORT:-8443}/ || exit 1

CMD ["node", "server.js"]

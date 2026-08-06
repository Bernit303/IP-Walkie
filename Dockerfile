FROM node:20-alpine

RUN apk add --no-cache openssl

WORKDIR /app
COPY package*.json ./
RUN npm ci --omit=dev
COPY server.js ./
COPY public ./public

EXPOSE 8443
CMD ["node", "server.js"]

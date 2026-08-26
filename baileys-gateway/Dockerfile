FROM node:20-bookworm-slim

WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci --omit=dev
COPY src ./src

ENV NODE_ENV=production
ENV PORT=8080
ENV BAILEYS_AUTH_DIR=/data/sessions
VOLUME ["/data"]
EXPOSE 8080

CMD ["npm", "start"]

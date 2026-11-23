FROM node:22-bookworm-slim AS build
WORKDIR /app
ARG BIS_BACKEND_URL
ENV NEXT_TELEMETRY_DISABLED=1
COPY package.json package-lock.json ./
RUN npm ci
COPY . .
RUN test -n "$BIS_BACKEND_URL" && npm run build

FROM node:22-bookworm-slim
WORKDIR /app
ARG BIS_BACKEND_URL
ENV BIS_BACKEND_URL=$BIS_BACKEND_URL
ENV NODE_ENV=production NEXT_TELEMETRY_DISABLED=1
COPY --from=build /app/package*.json ./
RUN npm ci --omit=dev
COPY --from=build --chown=node:node /app/.next ./.next
COPY --from=build /app/next.config.js ./
USER node
EXPOSE 3000
CMD ["npm", "start", "--", "--hostname", "0.0.0.0"]

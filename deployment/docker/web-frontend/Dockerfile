FROM node:22-alpine AS deps

WORKDIR /workspace/apps/web
COPY apps/web/package.json apps/web/package-lock.json ./
RUN npm ci

FROM node:22-alpine AS builder

WORKDIR /workspace/apps/web
ENV NEXT_TELEMETRY_DISABLED=1
COPY --from=deps /workspace/apps/web/node_modules ./node_modules
COPY apps/web ./
COPY assets /workspace/assets
COPY i18n /workspace/i18n
COPY packages/app-platform /workspace/packages/app-platform
COPY packages/contracts /workspace/packages/contracts
RUN npm run build

FROM node:22-alpine AS runner

WORKDIR /app
ENV NODE_ENV=production
ENV NEXT_TELEMETRY_DISABLED=1
COPY --from=builder /workspace/apps/web/.next/standalone ./
COPY --from=builder /workspace/apps/web/.next/static ./.next/static
COPY --from=builder /workspace/apps/web/public ./public

EXPOSE 3000

CMD ["node", "server.js"]

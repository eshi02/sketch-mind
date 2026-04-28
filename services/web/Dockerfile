FROM node:20-slim AS builder
WORKDIR /app
COPY package*.json ./
RUN npm install
COPY . .
ARG NEXT_PUBLIC_API_URL
ARG NEXT_PUBLIC_GOOGLE_CLIENT_ID
RUN if [ -n "$NEXT_PUBLIC_API_URL" ]; then \
      echo "NEXT_PUBLIC_API_URL=$NEXT_PUBLIC_API_URL" >> .env.production; \
    fi && \
    if [ -n "$NEXT_PUBLIC_GOOGLE_CLIENT_ID" ]; then \
      echo "NEXT_PUBLIC_GOOGLE_CLIENT_ID=$NEXT_PUBLIC_GOOGLE_CLIENT_ID" >> .env.production; \
    fi && \
    npm run build

FROM node:20-slim
WORKDIR /app
COPY --from=builder /app/.next ./.next
COPY --from=builder /app/node_modules ./node_modules
COPY --from=builder /app/package.json .
EXPOSE 3000
CMD ["npm", "start"]

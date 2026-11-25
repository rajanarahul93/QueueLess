FROM node:24-alpine
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci
COPY . .
# NEXT_PUBLIC_* values are inlined at build time. The build stops when NEXT_PUBLIC_API_URL is
# empty (scripts/check-build-env.mjs); ALLOW_LOCALHOST_API=true opts out for checks and tests.
ARG NEXT_PUBLIC_API_URL
ARG NEXT_PUBLIC_DEMO_MODE
ARG ALLOW_LOCALHOST_API
ENV NEXT_PUBLIC_API_URL=$NEXT_PUBLIC_API_URL
ENV NEXT_PUBLIC_DEMO_MODE=$NEXT_PUBLIC_DEMO_MODE
ENV ALLOW_LOCALHOST_API=$ALLOW_LOCALHOST_API
RUN npm run build
EXPOSE 3000
CMD ["npm", "start"]

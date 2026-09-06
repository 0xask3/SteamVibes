# Frontend: Vite build, then nginx serving the static output.
#
# VITE_API_URL is baked in at BUILD time, not read at runtime - Vite inlines
# import.meta.env into the bundle. The default in src/api.ts is
# http://127.0.0.1:8000, which is correct here because the browser runs on the
# host and reaches the backend through its published port. It is an ARG rather
# than a hardcoded value so a deployment behind a real domain can override it.

FROM node:22-alpine AS build

WORKDIR /app

# npm ci, not npm install: it installs exactly what package-lock.json says and
# fails if the two disagree, which is the point of having a lockfile.
COPY package.json package-lock.json ./
RUN npm ci

COPY . .

ARG VITE_API_URL
ENV VITE_API_URL=${VITE_API_URL}
RUN npm run build

FROM nginx:alpine

COPY --from=build /app/dist /usr/share/nginx/html
COPY nginx.conf /etc/nginx/conf.d/default.conf

EXPOSE 80

import axios from "axios";
import { getBasePath } from "./basePath.js";

const api = axios.create({
  
  baseURL: getBasePath(),
  timeout: 120000,
});


api.interceptors.request.use((config) => {
  const token = localStorage.getItem("panelToken");
  if (token) {
    config.headers.Authorization = `Bearer ${token}`;
  }
  return config;
});


api.interceptors.response.use(
  (response) => response,
  (error) => {
    if (error.response?.status === 401) {
      localStorage.removeItem("panelToken");
      window.location.href = getBasePath();
    }
    return Promise.reject(error);
  }
);

export default api;

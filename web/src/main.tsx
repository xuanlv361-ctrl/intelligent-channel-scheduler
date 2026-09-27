import React from 'react';import ReactDOM from 'react-dom/client';import {BrowserRouter} from 'react-router-dom';import {QueryClient,QueryClientProvider} from '@tanstack/react-query';import App from './App';import AppErrorBoundary from './components/AppErrorBoundary';import {ApiError,initializeEnterpriseSession} from './lib/apiClient';import './theme.css';
// Warm the opaque server-side session. API calls still await the same promise,
// so React StrictMode cannot create duplicate bootstrap sessions.
void initializeEnterpriseSession().catch(()=>undefined);
const queryClient=new QueryClient({defaultOptions:{queries:{retry:(count,error)=>error instanceof ApiError&&Boolean(error.detail.retryable)&&count<3,retryDelay:attempt=>Math.min(250*2**attempt,1000)}}});
ReactDOM.createRoot(document.getElementById('root')!).render(<React.StrictMode><AppErrorBoundary><QueryClientProvider client={queryClient}><BrowserRouter><App/></BrowserRouter></QueryClientProvider></AppErrorBoundary></React.StrictMode>);

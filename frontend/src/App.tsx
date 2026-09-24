import { Component, createContext, useContext, useState, type ReactNode } from 'react';
import { Link, NavLink, Route, Routes } from 'react-router-dom';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { Activity, CheckCheck, Database, LayoutList, Moon, ShieldCheck, Sun, Workflow } from 'lucide-react';
import { activeRole, api, setApiRole } from './api';
import { EmptyState, ErrorState } from './components';
import type { Health, Role } from './types';
import Queue from './pages/Queue';
import CaseDetail from './pages/CaseDetail';
import Approvals from './pages/Approvals';
export const RoleContext = createContext<{role:Role;setRole:(value:Role)=>void}>({role:'viewer',setRole:()=>{}});
export const useRole = () => useContext(RoleContext);
function ServiceHealth() {
 const query = useQuery({queryKey:['health'],queryFn:()=>api<Health>('/health'),refetchInterval:60000});
 const health = query.data;
 const graph = health ? Object.entries(health.tigergraph).map(([k,v])=>`${k.toUpperCase()}: ${v}`).join(', ') : 'Checking service state';
 const values = [
  {name:'TigerGraph',ok:health && Object.values(health.tigergraph).every(s=> /^(up|online|running|healthy)$/i.test(s)),info:graph},
  {name:'MCP',ok:health && /^(up|online|reachable|ready|healthy)$/i.test(health.mcp.status),info:health?.mcp.detail || 'Checking MCP connectivity'},
  {name:'Gemini',ok:health && /^(ready|configured|available)$/i.test(health.gemini.status),info:health ? `${health.gemini.status}; configuration and quota cache only, no live model probe. ${health.gemini.models?.join(', ') || ''}` : 'Checking cached quota state'},
  {name:'Vectors',ok:health && health.vector_indexes.some(x=>x.count>0),info:health ? health.vector_indexes.map(x=>`${x.id}: ${x.count.toLocaleString()} checkpoint IDs`).join('; ') + '. Checkpoints are not a live graph inventory.' : 'Reading upload checkpoints'}
 ];
 return <div className="service-health" aria-label="Service health">{values.map(value=><span key={value.name} tabIndex={0} title={query.isError ? 'Health request unavailable; saved cases remain readable.' : value.info} className={`service ${query.isError ? 'bad' : value.ok ? 'good' : 'unknown'}`}><span className="dot"/>{value.name}<span className="sr-only">: {query.isError?'unavailable':value.info}</span></span>)}</div>;
}
class ErrorBoundary extends Component<{children:ReactNode},{error:Error|null}> {
 state={error:null as Error|null};
 static getDerivedStateFromError(error:Error){return {error};}
 render(){return this.state.error ? <main className="workspace"><ErrorState error={this.state.error} retry={()=>location.reload()}/></main> : this.props.children;}
}
export default function App() {
 const [role,setRoleState]=useState<Role>(['viewer','analyst_l1','supervisor_l2'].includes(activeRole)?activeRole:'viewer');
 const [theme,setTheme]=useState(()=>localStorage.getItem('fraudloop-theme')||'dark');
 document.documentElement.dataset.theme=theme;
 const client=useQueryClient();
 const setRole=(value:Role)=>{setRoleState(value);setApiRole(value);void client.invalidateQueries({queryKey:['approvals']});};
 return <RoleContext.Provider value={{role,setRole}}><div className="app-shell"><a className="skip-link" href="#main">Skip to content</a>
  <header className="topbar"><Link to="/" className="wordmark" aria-label="FRAUDLOOP home"><Workflow size={27} strokeWidth={2.1} aria-hidden/><span>FRAUDLOOP</span></Link><span className="workspace-name">Investigation console</span><ServiceHealth/><div className="topbar-controls"><label className="sr-only" htmlFor="role">Demo role</label><select id="role" aria-label="Demo role" value={role} onChange={e=>setRole(e.target.value as Role)}><option value="viewer">Viewer</option><option value="analyst_l1">Analyst · L1</option><option value="supervisor_l2">Supervisor · L2</option></select><button className="icon-button" aria-label={theme==='dark'?'Switch to light theme':'Switch to dark theme'} onClick={()=>{const next=theme==='dark'?'light':'dark';setTheme(next);localStorage.setItem('fraudloop-theme',next);}}>{theme==='dark'?<Sun size={18}/>:<Moon size={18}/>}</button></div></header>
  <aside className="sidebar"><nav aria-label="Main navigation"><NavLink to="/" end><LayoutList size={18} aria-hidden/>Case queue</NavLink><NavLink to="/approvals"><CheckCheck size={18} aria-hidden/>Approvals</NavLink><NavLink to="/memory"><Database size={18} aria-hidden/>Case memory</NavLink></nav><div className="sidebar-note"><ShieldCheck size={20} aria-hidden/><strong>Policy led. Evidence first.</strong><p>Bank actions remain recommendations until approved. No external actions are executed.</p><span className="demo-label">Local demo roles</span></div><div className="sidebar-bottom"><Activity size={14} aria-hidden/>HHGOA · IEEE-CIS 2016</div></aside>
  <ErrorBoundary><main className="workspace" id="main"><Routes><Route path="/" element={<Queue/>}/><Route path="/cases/:id" element={<CaseDetail/>}/><Route path="/approvals" element={<Approvals/>}/><Route path="*" element={<EmptyState title="Page unavailable"><Link to="/">Return to the case queue</Link></EmptyState>}/></Routes></main></ErrorBoundary>
 </div></RoleContext.Provider>;
}

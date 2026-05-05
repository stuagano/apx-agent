import { useQuery, useSuspenseQuery, useMutation } from "@tanstack/react-query";
import type { UseQueryOptions, UseSuspenseQueryOptions, UseMutationOptions } from "@tanstack/react-query";
export class ApiError extends Error {
    status: number;
    statusText: string;
    body: unknown;
    constructor(status: number, statusText: string, body: unknown){
        super(`HTTP ${status}: ${statusText}`);
        this.name = "ApiError";
        this.status = status;
        this.statusText = statusText;
        this.body = body;
    }
}
export interface AgentCard {
    description: string;
    display_name: string;
    id: string;
    last_seen?: string | null;
    mcp_endpoint?: string | null;
    name: string;
    status: string;
    supports_invoke?: boolean;
    tags?: string[];
    tools: AgentTool[];
    url: string;
}
export interface AgentTool {
    description: string;
    name: string;
}
export interface ComplexValue {
    display?: string | null;
    primary?: boolean | null;
    ref?: string | null;
    type?: string | null;
    value?: string | null;
}
export interface HTTPValidationError {
    detail?: ValidationError[];
}
export interface InvokeRequest {
    input: string;
}
export interface Name {
    family_name?: string | null;
    given_name?: string | null;
}
export interface RegisterRequest {
    tags?: string[];
    url: string;
}
export interface User {
    active?: boolean | null;
    display_name?: string | null;
    emails?: ComplexValue[] | null;
    entitlements?: ComplexValue[] | null;
    external_id?: string | null;
    groups?: ComplexValue[] | null;
    id?: string | null;
    name?: Name | null;
    roles?: ComplexValue[] | null;
    schemas?: UserSchema[] | null;
    user_name?: string | null;
}
export const UserSchema = {
    "urn:ietf:params:scim:schemas:core:2.0:User": "urn:ietf:params:scim:schemas:core:2.0:User",
    "urn:ietf:params:scim:schemas:extension:workspace:2.0:User": "urn:ietf:params:scim:schemas:extension:workspace:2.0:User"
} as const;
export type UserSchema = typeof UserSchema[keyof typeof UserSchema];
export interface ValidationError {
    ctx?: Record<string, unknown>;
    input?: unknown;
    loc: (string | number)[];
    msg: string;
    type: string;
}
export interface VersionOut {
    version: string;
}
export interface ListAgentsParams {
    status?: string | null;
}
export const listAgents = async (params?: ListAgentsParams, options?: RequestInit): Promise<{
    data: AgentCard[];
}> =>{
    const searchParams = new URLSearchParams();
    if (params?.status != null) searchParams.set("status", String(params?.status));
    const queryString = searchParams.toString();
    const url = queryString ? `/api/agents?${queryString}` : "/api/agents";
    const res = await fetch(url, {
        ...options,
        method: "GET"
    });
    if (!res.ok) {
        const body = await res.text();
        let parsed: unknown;
        try {
            parsed = JSON.parse(body);
        } catch  {
            parsed = body;
        }
        throw new ApiError(res.status, res.statusText, parsed);
    }
    return {
        data: await res.json()
    };
};
export const listAgentsKey = (params?: ListAgentsParams)=>{
    return [
        "/api/agents",
        params
    ] as const;
};
export function useListAgents<TData = {
    data: AgentCard[];
}>(options?: {
    params?: ListAgentsParams;
    query?: Omit<UseQueryOptions<{
        data: AgentCard[];
    }, ApiError, TData>, "queryKey" | "queryFn">;
}) {
    return useQuery({
        queryKey: listAgentsKey(options?.params),
        queryFn: ()=>listAgents(options?.params),
        ...options?.query
    });
}
export function useListAgentsSuspense<TData = {
    data: AgentCard[];
}>(options?: {
    params?: ListAgentsParams;
    query?: Omit<UseSuspenseQueryOptions<{
        data: AgentCard[];
    }, ApiError, TData>, "queryKey" | "queryFn">;
}) {
    return useSuspenseQuery({
        queryKey: listAgentsKey(options?.params),
        queryFn: ()=>listAgents(options?.params),
        ...options?.query
    });
}
export const refreshAllAgents = async (options?: RequestInit): Promise<{
    data: AgentCard[];
}> =>{
    const res = await fetch("/api/agents/refresh-all", {
        ...options,
        method: "POST"
    });
    if (!res.ok) {
        const body = await res.text();
        let parsed: unknown;
        try {
            parsed = JSON.parse(body);
        } catch  {
            parsed = body;
        }
        throw new ApiError(res.status, res.statusText, parsed);
    }
    return {
        data: await res.json()
    };
};
export function useRefreshAllAgents(options?: {
    mutation?: UseMutationOptions<{
        data: AgentCard[];
    }, ApiError, void>;
}) {
    return useMutation({
        mutationFn: ()=>refreshAllAgents(),
        ...options?.mutation
    });
}
export const registerAgent = async (data: RegisterRequest, options?: RequestInit): Promise<{
    data: AgentCard;
}> =>{
    const res = await fetch("/api/agents/register", {
        ...options,
        method: "POST",
        headers: {
            "Content-Type": "application/json",
            ...options?.headers
        },
        body: JSON.stringify(data)
    });
    if (!res.ok) {
        const body = await res.text();
        let parsed: unknown;
        try {
            parsed = JSON.parse(body);
        } catch  {
            parsed = body;
        }
        throw new ApiError(res.status, res.statusText, parsed);
    }
    return {
        data: await res.json()
    };
};
export function useRegisterAgent(options?: {
    mutation?: UseMutationOptions<{
        data: AgentCard;
    }, ApiError, RegisterRequest>;
}) {
    return useMutation({
        mutationFn: (data)=>registerAgent(data),
        ...options?.mutation
    });
}
export interface GetAgentParams {
    agent_id: string;
}
export const getAgent = async (params: GetAgentParams, options?: RequestInit): Promise<{
    data: AgentCard;
}> =>{
    const res = await fetch(`/api/agents/${params.agent_id}`, {
        ...options,
        method: "GET"
    });
    if (!res.ok) {
        const body = await res.text();
        let parsed: unknown;
        try {
            parsed = JSON.parse(body);
        } catch  {
            parsed = body;
        }
        throw new ApiError(res.status, res.statusText, parsed);
    }
    return {
        data: await res.json()
    };
};
export const getAgentKey = (params?: GetAgentParams)=>{
    return [
        "/api/agents/{agent_id}",
        params
    ] as const;
};
export function useGetAgent<TData = {
    data: AgentCard;
}>(options: {
    params: GetAgentParams;
    query?: Omit<UseQueryOptions<{
        data: AgentCard;
    }, ApiError, TData>, "queryKey" | "queryFn">;
}) {
    return useQuery({
        queryKey: getAgentKey(options.params),
        queryFn: ()=>getAgent(options.params),
        ...options?.query
    });
}
export function useGetAgentSuspense<TData = {
    data: AgentCard;
}>(options: {
    params: GetAgentParams;
    query?: Omit<UseSuspenseQueryOptions<{
        data: AgentCard;
    }, ApiError, TData>, "queryKey" | "queryFn">;
}) {
    return useSuspenseQuery({
        queryKey: getAgentKey(options.params),
        queryFn: ()=>getAgent(options.params),
        ...options?.query
    });
}
export interface DeregisterAgentParams {
    agent_id: string;
}
export const deregisterAgent = async (params: DeregisterAgentParams, options?: RequestInit): Promise<{
    data: unknown;
}> =>{
    const res = await fetch(`/api/agents/${params.agent_id}`, {
        ...options,
        method: "DELETE"
    });
    if (!res.ok) {
        const body = await res.text();
        let parsed: unknown;
        try {
            parsed = JSON.parse(body);
        } catch  {
            parsed = body;
        }
        throw new ApiError(res.status, res.statusText, parsed);
    }
    return {
        data: await res.json()
    };
};
export function useDeregisterAgent(options?: {
    mutation?: UseMutationOptions<{
        data: unknown;
    }, ApiError, {
        params: DeregisterAgentParams;
    }>;
}) {
    return useMutation({
        mutationFn: (vars)=>deregisterAgent(vars.params),
        ...options?.mutation
    });
}
export interface GetAgentA2ACardParams {
    agent_id: string;
}
export const getAgentA2ACard = async (params: GetAgentA2ACardParams, options?: RequestInit): Promise<{
    data: unknown;
}> =>{
    const res = await fetch(`/api/agents/${params.agent_id}/card`, {
        ...options,
        method: "GET"
    });
    if (!res.ok) {
        const body = await res.text();
        let parsed: unknown;
        try {
            parsed = JSON.parse(body);
        } catch  {
            parsed = body;
        }
        throw new ApiError(res.status, res.statusText, parsed);
    }
    return {
        data: await res.json()
    };
};
export const getAgentA2ACardKey = (params?: GetAgentA2ACardParams)=>{
    return [
        "/api/agents/{agent_id}/card",
        params
    ] as const;
};
export function useGetAgentA2ACard<TData = {
    data: unknown;
}>(options: {
    params: GetAgentA2ACardParams;
    query?: Omit<UseQueryOptions<{
        data: unknown;
    }, ApiError, TData>, "queryKey" | "queryFn">;
}) {
    return useQuery({
        queryKey: getAgentA2ACardKey(options.params),
        queryFn: ()=>getAgentA2ACard(options.params),
        ...options?.query
    });
}
export function useGetAgentA2ACardSuspense<TData = {
    data: unknown;
}>(options: {
    params: GetAgentA2ACardParams;
    query?: Omit<UseSuspenseQueryOptions<{
        data: unknown;
    }, ApiError, TData>, "queryKey" | "queryFn">;
}) {
    return useSuspenseQuery({
        queryKey: getAgentA2ACardKey(options.params),
        queryFn: ()=>getAgentA2ACard(options.params),
        ...options?.query
    });
}
export interface InvokeAgentParams {
    agent_id: string;
}
export const invokeAgent = async (params: InvokeAgentParams, data: InvokeRequest, options?: RequestInit): Promise<{
    data: unknown;
}> =>{
    const res = await fetch(`/api/agents/${params.agent_id}/invoke`, {
        ...options,
        method: "POST",
        headers: {
            "Content-Type": "application/json",
            ...options?.headers
        },
        body: JSON.stringify(data)
    });
    if (!res.ok) {
        const body = await res.text();
        let parsed: unknown;
        try {
            parsed = JSON.parse(body);
        } catch  {
            parsed = body;
        }
        throw new ApiError(res.status, res.statusText, parsed);
    }
    return {
        data: await res.json()
    };
};
export function useInvokeAgent(options?: {
    mutation?: UseMutationOptions<{
        data: unknown;
    }, ApiError, {
        params: InvokeAgentParams;
        data: InvokeRequest;
    }>;
}) {
    return useMutation({
        mutationFn: (vars)=>invokeAgent(vars.params, vars.data),
        ...options?.mutation
    });
}
export interface RefreshAgentParams {
    agent_id: string;
}
export const refreshAgent = async (params: RefreshAgentParams, options?: RequestInit): Promise<{
    data: AgentCard;
}> =>{
    const res = await fetch(`/api/agents/${params.agent_id}/refresh`, {
        ...options,
        method: "POST"
    });
    if (!res.ok) {
        const body = await res.text();
        let parsed: unknown;
        try {
            parsed = JSON.parse(body);
        } catch  {
            parsed = body;
        }
        throw new ApiError(res.status, res.statusText, parsed);
    }
    return {
        data: await res.json()
    };
};
export function useRefreshAgent(options?: {
    mutation?: UseMutationOptions<{
        data: AgentCard;
    }, ApiError, {
        params: RefreshAgentParams;
    }>;
}) {
    return useMutation({
        mutationFn: (vars)=>refreshAgent(vars.params),
        ...options?.mutation
    });
}
export interface CurrentUserParams {
    "X-Forwarded-Host"?: string | null;
    "X-Forwarded-Preferred-Username"?: string | null;
    "X-Forwarded-User"?: string | null;
    "X-Forwarded-Email"?: string | null;
    "X-Request-Id"?: string | null;
    "X-Forwarded-Access-Token"?: string | null;
}
export const currentUser = async (params?: CurrentUserParams, options?: RequestInit): Promise<{
    data: User;
}> =>{
    const res = await fetch("/api/current-user", {
        ...options,
        method: "GET",
        headers: {
            ...(params?.["X-Forwarded-Host"] != null && {
                "X-Forwarded-Host": params["X-Forwarded-Host"]
            }),
            ...(params?.["X-Forwarded-Preferred-Username"] != null && {
                "X-Forwarded-Preferred-Username": params["X-Forwarded-Preferred-Username"]
            }),
            ...(params?.["X-Forwarded-User"] != null && {
                "X-Forwarded-User": params["X-Forwarded-User"]
            }),
            ...(params?.["X-Forwarded-Email"] != null && {
                "X-Forwarded-Email": params["X-Forwarded-Email"]
            }),
            ...(params?.["X-Request-Id"] != null && {
                "X-Request-Id": params["X-Request-Id"]
            }),
            ...(params?.["X-Forwarded-Access-Token"] != null && {
                "X-Forwarded-Access-Token": params["X-Forwarded-Access-Token"]
            }),
            ...options?.headers
        }
    });
    if (!res.ok) {
        const body = await res.text();
        let parsed: unknown;
        try {
            parsed = JSON.parse(body);
        } catch  {
            parsed = body;
        }
        throw new ApiError(res.status, res.statusText, parsed);
    }
    return {
        data: await res.json()
    };
};
export const currentUserKey = (params?: CurrentUserParams)=>{
    return [
        "/api/current-user",
        params
    ] as const;
};
export function useCurrentUser<TData = {
    data: User;
}>(options?: {
    params?: CurrentUserParams;
    query?: Omit<UseQueryOptions<{
        data: User;
    }, ApiError, TData>, "queryKey" | "queryFn">;
}) {
    return useQuery({
        queryKey: currentUserKey(options?.params),
        queryFn: ()=>currentUser(options?.params),
        ...options?.query
    });
}
export function useCurrentUserSuspense<TData = {
    data: User;
}>(options?: {
    params?: CurrentUserParams;
    query?: Omit<UseSuspenseQueryOptions<{
        data: User;
    }, ApiError, TData>, "queryKey" | "queryFn">;
}) {
    return useSuspenseQuery({
        queryKey: currentUserKey(options?.params),
        queryFn: ()=>currentUser(options?.params),
        ...options?.query
    });
}
export const version = async (options?: RequestInit): Promise<{
    data: VersionOut;
}> =>{
    const res = await fetch("/api/version", {
        ...options,
        method: "GET"
    });
    if (!res.ok) {
        const body = await res.text();
        let parsed: unknown;
        try {
            parsed = JSON.parse(body);
        } catch  {
            parsed = body;
        }
        throw new ApiError(res.status, res.statusText, parsed);
    }
    return {
        data: await res.json()
    };
};
export const versionKey = ()=>{
    return [
        "/api/version"
    ] as const;
};
export function useVersion<TData = {
    data: VersionOut;
}>(options?: {
    query?: Omit<UseQueryOptions<{
        data: VersionOut;
    }, ApiError, TData>, "queryKey" | "queryFn">;
}) {
    return useQuery({
        queryKey: versionKey(),
        queryFn: ()=>version(),
        ...options?.query
    });
}
export function useVersionSuspense<TData = {
    data: VersionOut;
}>(options?: {
    query?: Omit<UseSuspenseQueryOptions<{
        data: VersionOut;
    }, ApiError, TData>, "queryKey" | "queryFn">;
}) {
    return useSuspenseQuery({
        queryKey: versionKey(),
        queryFn: ()=>version(),
        ...options?.query
    });
}

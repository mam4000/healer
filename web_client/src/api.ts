import axios from 'axios';

// --- Types ---

export interface MoleculeRequest {
    molecule: string;
    bb_source: string;
    reaction_tags: string[];
    custom_sites?: [number, number][]; // List of tuples
    sim_threshold: number;
    n_compositions: number;
    randomize_compositions: boolean;
    random_seed: number;
    retro_tree_depth: number;
    min_frag_size: number;
    
    max_bbs_per_frag: number;
    shuffle_bb_order: boolean;
    
    max_evals_per_comp?: number;
    max_products_per_comp?: number;
    max_total_products?: number;
    
    use_fragment_healer: boolean;
}

export interface SiteRequest {
    molecule: string;
    bb_source: string;
    reaction_tags: string[];
    reactive_sites?: number[];
    rules?: Record<string, [number, number]>;
    struct_rules?: string[];
    
    shuffle_bb_order: boolean;
    
    max_evals_per_comp?: number;
    max_products_per_comp?: number;
    max_total_products?: number;
}

export interface JobSubmitResponse {
    job_id: string;
    status: string;
}

export interface EnumerationResult {
    Product: string;
    Similarity_to_query: number;
    Reaction_name?: string;
    [key: string]: any; // For BB1, BB2, etc.
}

export interface JobResult {
    display: EnumerationResult[];
    complete: any[];
}

export interface JobStatusResponse {
    job_id: string;
    status: 'PENDING' | 'STARTED' | 'SUCCESS' | 'FAILURE' | 'RETRY';
    result?: JobResult;
    error?: string;
}

// --- API Functions ---

const api = axios.create({
    baseURL: '/api',
});

export const submitMoleculeJob = async (data: MoleculeRequest) => {
    const res = await api.post<JobSubmitResponse>('/enumerate/molecule', data);
    return res.data;
};

export const submitSiteJob = async (data: SiteRequest) => {
    const res = await api.post<JobSubmitResponse>('/enumerate/site', data);
    return res.data;
};

export const getJobStatus = async (jobId: string) => {
    const res = await api.get<JobStatusResponse>(`/jobs/${jobId}`);
    return res.data;
};

export const cancelJob = async (jobId: string) => {
    const res = await api.post<{ job_id: string; status: string }>(`/jobs/${jobId}/cancel`);
    return res.data;
};

export const getServerMode = async () => {
    const res = await api.get<{ mode: 'tasks' | 'local' }>('/info/mode');
    return res.data.mode;
};

export interface ServerLimits {
    max_evals_per_comp: number;
    max_products_per_comp: number;
    max_total_products: number;
    sim_threshold_min: number;
    sim_threshold_max: number;
    max_bbs_per_frag: number;
    n_compositions_max: number;
    retro_depth_max: number;
    min_frag_size_min: number;
    max_reaction_tags: number;
}

export interface ServerLimitsResponse {
    server_mode: boolean;
    limits: ServerLimits;
}

export const getServerLimits = async () => {
    const res = await api.get<ServerLimitsResponse>('/info/limits');
    return res.data;
};

export interface BuildingBlockOption {
    value: string;
    label: string;
}

export const getBuildingBlocks = async () => {
    const res = await api.get<{ building_blocks: BuildingBlockOption[] }>('/info/building-blocks');
    return res.data.building_blocks;
};

export const convertSmilesToMol = async (smiles: string) => {
    const res = await api.post<{ molblock: string }>('/utils/smiles-to-mol', { smiles });
    return res.data.molblock;
};

export const getReactionTags = async () => {
    const res = await api.get<string[]>('/utils/reaction-tags');
    return res.data;
};

export const getMolWithIndices = async (smiles: string) => {
    const res = await api.post<{ svg: string, properties: any }>('/utils/render-mol-with-indices', { smiles });
    return res.data;
};

export const getMolResult = async (smiles: string, bbs?: string[], alpha?: number, bgColor?: string) => {
    const res = await api.post<{ svg: string }>('/utils/render-result', { smiles, bbs, alpha, bgColor });
    return res.data.svg;
};

export const getDownloadUrl = (jobId: string) => `/api/jobs/${jobId}/download`;

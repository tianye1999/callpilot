enum RootPresentation: Equatable {
    case pairing
    case main
    case incomingOffer(InboundOffer)
    case call

    static func resolve(
        isPaired: Bool,
        callState: CallState,
        incomingOffer: InboundOffer?
    ) -> RootPresentation {
        guard isPaired else { return .pairing }
        if callState.isCallPresented { return .call }
        if let incomingOffer { return .incomingOffer(incomingOffer) }
        return .main
    }
}
